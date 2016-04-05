#!/usr/bin/env python

import datetime
import json
import os
import sys
import time

import acme.challenges
import acme.client
import acme.jose

import click

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

import boto3

import OpenSSL.crypto

import rfc3986

import botocore

DEFAULT_ACME_DIRECTORY_URL = "https://acme-v01.api.letsencrypt.org/directory"
CERTIFICATE_EXPIRATION_THRESHOLD = datetime.timedelta(days=45)
# One day
PERSISTENT_SLEEP_INTERVAL = 60 * 60 * 24
DNS_TTL = 30


class Logger(object):
    def __init__(self):
        self._out = sys.stdout

    def emit(self, event, **data):
        formatted_data = " ".join(
            "{}={!r}".format(k, v) for k, v in data.iteritems()
        )
        self._out.write("{} [{}] {}\n".format(
            datetime.datetime.utcnow().replace(microsecond=0),
            event,
            formatted_data
        ))


def generate_rsa_private_key():
    return rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )


def generate_ecdsa_private_key():
    return ec.generate_private_key(ec.SECP256R1(), backend=default_backend())


def generate_csr(private_key, hosts):
    csr_builder = x509.CertificateSigningRequestBuilder().subject_name(
        # This is the same thing the official letsencrypt client does.
        x509.Name([
            x509.NameAttribute(x509.NameOID.COMMON_NAME, hosts[0]),
        ])
    ).add_extension(
        x509.SubjectAlternativeName([
            x509.DNSName(host)
            for host in hosts
        ]),
        # TODO: change to `critical=True` when Let's Encrypt supports it.
        critical=False,
    )
    return csr_builder.sign(private_key, hashes.SHA256(), default_backend())


def find_dns_challenge(authz):
    for combo in authz.body.resolved_combinations:
        if (
            len(combo) == 1 and
            isinstance(combo[0].chall, acme.challenges.DNS01)
        ):
            yield combo[0]


def find_zone_id_for_domain(route53_client, domain):
    for page in route53_client.get_paginator("list_hosted_zones").paginate():
        for zone in page["HostedZones"]:
            # This assumes that zones are returned sorted by specificity,
            # meaning in the following order:
            # ["foo.bar.baz.com", "bar.baz.com", "baz.com", "com"]
            if (
                domain.endswith(zone["Name"]) or
                (domain + ".").endswith(zone["Name"])
            ):
                return zone["Id"]


def wait_for_route53_change(route53_client, change_id):
    while True:
        response = route53_client.get_change(Id=change_id)
        if response["ChangeInfo"]["Status"] == "INSYNC":
            return
        time.sleep(5)


def change_txt_record(route53_client, action, zone_id, domain, value):
    response = route53_client.change_resource_record_sets(
        HostedZoneId=zone_id,
        ChangeBatch={
            "Changes": [
                {
                    "Action": action,
                    "ResourceRecordSet": {
                        "Name": domain,
                        "Type": "TXT",
                        "TTL": DNS_TTL,
                        "ResourceRecords": [
                            # For some reason TXT records need to be manually
                            # quoted.
                            {"Value": '"{}"'.format(value)}
                        ],
                    }
                }
            ]
        }
    )
    return response["ChangeInfo"]["Id"]


def generate_certificate_name(hosts, cert):
    return "{serial}-{expiration}-{hosts}".format(
        serial=cert.serial,
        expiration=cert.not_valid_after.date(),
        hosts="-".join(h.replace(".", "_") for h in hosts),
    )[:128]


def get_load_balancer_certificate(elb_client, elb_name, listener):
    elb_port = listener.get("load_balancer_port", 443)
    response = elb_client.describe_load_balancers(
        LoadBalancerNames=[elb_name]
    )
    [description] = response["LoadBalancerDescriptions"]

    for listener in description["ListenerDescriptions"]:
        if listener["Listener"]["LoadBalancerPort"] == elb_port:
            return listener["Listener"]["SSLCertificateId"]

    return False


def get_expiration_date_for_certificate(iam_client, ssl_certificate_arn):
    paginator = iam_client.get_paginator("list_server_certificates").paginate()
    for page in paginator:
        for server_certificate in page["ServerCertificateMetadataList"]:
            if server_certificate["Arn"] == ssl_certificate_arn:
                return server_certificate["Expiration"]


class AuthorizationRecord(object):
    def __init__(self, host, authz, dns_challenge, route53_change_id,
                 route53_zone_id):
        self.host = host
        self.authz = authz
        self.dns_challenge = dns_challenge
        self.route53_change_id = route53_change_id
        self.route53_zone_id = route53_zone_id


def start_dns_challenge(logger, acme_client, elb_client, route53_client,
                        elb_name, host):
    logger.emit(
        "updating-elb.request-acme-challenge", elb_name=elb_name, host=host
    )
    authz = acme_client.request_domain_challenges(
        host, acme_client.directory.new_authz
    )

    [dns_challenge] = find_dns_challenge(authz)

    zone_id = find_zone_id_for_domain(route53_client, host)
    logger.emit(
        "updating-elb.create-txt-record", elb_name=elb_name, host=host
    )
    change_id = change_txt_record(
        route53_client,
        "CREATE",
        zone_id,
        dns_challenge.validation_domain_name(host),
        dns_challenge.validation(acme_client.key),
    )
    return AuthorizationRecord(
        host,
        authz,
        dns_challenge,
        change_id,
        zone_id,
    )


def complete_dns_challenge(logger, acme_client, route53_client, elb_name,
                           authz_record):
    logger.emit(
        "updating-elb.wait-for-route53",
        elb_name=elb_name, host=authz_record.host
    )
    wait_for_route53_change(route53_client, authz_record.route53_change_id)

    response = authz_record.dns_challenge.response(acme_client.key)

    logger.emit(
        "updating-elb.local-validation",
        elb_name=elb_name, host=authz_record.host
    )
    verified = response.simple_verify(
        authz_record.dns_challenge.chall,
        authz_record.host,
        acme_client.key.public_key()
    )
    if not verified:
        raise ValueError("Failed verification")

    logger.emit(
        "updating-elb.answer-challenge",
        elb_name=elb_name, host=authz_record.host
    )
    acme_client.answer_challenge(authz_record.dns_challenge, response)


def request_certificate(logger, acme_client, elb_name, authorizations, csr):
    logger.emit("updating-elb.request-cert", elb_name=elb_name)
    cert_response, _ = acme_client.poll_and_request_issuance(
        acme.jose.util.ComparableX509(
            OpenSSL.crypto.load_certificate_request(
                OpenSSL.crypto.FILETYPE_ASN1,
                csr.public_bytes(serialization.Encoding.DER),
            )
        ),
        authzrs=[authz_record.authz for authz_record in authorizations],
    )
    pem_certificate = OpenSSL.crypto.dump_certificate(
        OpenSSL.crypto.FILETYPE_PEM, cert_response.body
    )
    pem_certificate_chain = "\n".join(
        OpenSSL.crypto.dump_certificate(OpenSSL.crypto.FILETYPE_PEM, cert)
        for cert in acme_client.fetch_chain(cert_response)
    )
    return pem_certificate, pem_certificate_chain


def update_elb(logger, acme_client, elb_client, route53_client, iam_client,
               force_issue, elb_name, listener, hosts, key_type, cert_only,
               create_listener, fullchain_path, privatekey_path):
    logger.emit("updating-elb", elb_name=elb_name)

    if key_type == "rsa":
        private_key = generate_rsa_private_key()
    elif key_type == "ecdsa":
        private_key = generate_ecdsa_private_key()
    else:
        raise ValueError("Invalid key_type: {!r}".format(key_type))

    f=open(privatekey_path, 'w')
    f.write(private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    f.close()

    csr = generate_csr(private_key, hosts)

    authorizations = []
    try:
        for host in hosts:
            authz_record = start_dns_challenge(
                logger, acme_client, elb_client, route53_client, elb_name, host
            )
            authorizations.append(authz_record)

        for authz_record in authorizations:
            complete_dns_challenge(
                logger, acme_client, route53_client, elb_name, authz_record
            )

        pem_certificate, pem_certificate_chain = request_certificate(
            logger, acme_client, elb_name, authorizations, csr)

        f=open(fullchain_path, 'w')
        f.write(pem_certificate)
        f.write(pem_certificate_chain)
        f.close()
    finally:
        for authz_record in authorizations:
            logger.emit(
                "updating-elb.delete-txt-record",
                elb_name=elb_name, host=authz_record.host
            )
            dns_challenge = authz_record.dns_challenge
            change_txt_record(
                route53_client,
                "DELETE",
                authz_record.route53_zone_id,
                dns_challenge.validation_domain_name(authz_record.host),
                dns_challenge.validation(acme_client.key),
            )


def update_elbs(logger, acme_client, elb_client, route53_client, iam_client,
                force_issue, domains, cert_only, create_listener, fullchain_path, privatekey_path):
    for domain in domains:
        update_elb(
            logger,
            acme_client,
            elb_client,
            route53_client,
            iam_client,
            force_issue,
            domain["elb"]["name"],
            domain["elb"].get("listener", {'load_balancer_port': 443}),
            domain["hosts"],
            domain.get("key_type", "rsa"),
            cert_only,
            create_listener,
            fullchain_path,
            privatekey_path
        )


def setup_acme_client(s3_client, acme_directory_url, acme_account_key):
    uri = rfc3986.urlparse(acme_account_key)
    if uri.scheme == "file":
        with open(uri.path) as f:
            key = f.read()
    elif uri.scheme == "s3":
        # uri.path includes a leading "/"
        response = s3_client.get_object(Bucket=uri.host, Key=uri.path[1:])
        key = response["Body"].read()
    else:
        raise ValueError(
            "Invalid acme account key: {!r}".format(acme_account_key)
        )

    key = serialization.load_pem_private_key(
        key, password=None, backend=default_backend()
    )
    return acme_client_for_private_key(acme_directory_url, key)


def acme_client_for_private_key(acme_directory_url, private_key):
    return acme.client.Client(
        # TODO: support EC keys, when acme.jose does.
        acme_directory_url, key=acme.jose.JWKRSA(key=private_key)
    )


@click.group()
def cli():
    pass


@cli.command(name="update-certificates")
@click.option(
    "--force-issue", is_flag=True, help=(
        "Issue a new certificate, even if the old one isn't close to "
        "expiration."
    )
)
@click.option(
    "--cert-only", is_flag=True, help=(
        "Only issue the certificate. Do not attempt to add the certificate "
        "to the ELB."
    )
)
@click.option(
    "--create-listener", is_flag=True, help=(
        "Create the HTTPS listener if it is missing."
    )
)
def update_certificates(persistent=False, force_issue=False,
                        cert_only=False, create_listener=False):
    logger = Logger()
    logger.emit("startup")

    if persistent and force_issue:
        raise ValueError("Can't specify both --persistent and --force-issue")

    session = boto3.Session()
    s3_client = session.client("s3")
    elb_client = session.client("elb")
    route53_client = session.client("route53")
    iam_client = session.client("iam")

    # Structure: {
    #     "domains": [
    #         {"elb": {"name" "...", "listener": { ... }}, hosts: ["..."]}
    #     ],
    #     "acme_account_key": "s3://bucket/object",
    #     "acme_directory_url": "(optional)"
    # }
    config = json.loads(os.environ["LETSENCRYPT_AWS_CONFIG"])
    domains = config["domains"]
    acme_directory_url = config.get(
        "acme_directory_url", DEFAULT_ACME_DIRECTORY_URL
    )
    acme_account_key = config["acme_account_key"]
    fullchain_path = config["cert_fullchain_path"]
    privatekey_path = config["cert_privatekey_path"]

    acme_client = setup_acme_client(
        s3_client, acme_directory_url, acme_account_key
    )

    logger.emit("running", mode="single")
    update_elbs(
        logger, acme_client, elb_client, route53_client, iam_client,
        force_issue, domains, cert_only, create_listener, fullchain_path, privatekey_path
    )


@cli.command()
@click.argument("email")
@click.option(
    "--out",
    type=click.File("w"),
    default="-",
    help="Where to write the private key to. Defaults to stdout."
)
def register(email, out):
    logger = Logger()
    config = json.loads(os.environ.get("LETSENCRYPT_AWS_CONFIG", "{}"))
    acme_directory_url = config.get(
        "acme_directory_url", DEFAULT_ACME_DIRECTORY_URL
    )

    logger.emit("acme-register.generate-key")
    private_key = generate_rsa_private_key()
    acme_client = acme_client_for_private_key(acme_directory_url, private_key)

    logger.emit("acme-register.register", email=email)
    registration = acme_client.register(
        acme.messages.NewRegistration.from_data(email=email)
    )
    logger.emit("acme-register.agree-to-tos")
    acme_client.agree_to_tos(registration)
    out.write(private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))


if __name__ == "__main__":
    cli()
