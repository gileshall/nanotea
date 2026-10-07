"""A throwaway CA and one certificate it signs, for a run's proxies and push receiver.

python -m e2e.certs DIR NAME... : DIR/ca.crt, DIR/cert.pem, DIR/key.pem. A NAME that parses as an IP address
goes in as one; the rest as DNS names."""

import datetime
import ipaddress
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def make(out: Path, names: list[str]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc)
    start, end = now - datetime.timedelta(hours=1), now + datetime.timedelta(days=2)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (x509.CertificateBuilder().subject_name(_name("nanotea e2e CA")).issuer_name(_name("nanotea e2e CA"))
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(start).not_valid_after(end)
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                       content_commitment=False, key_encipherment=False, data_encipherment=False,
                                       key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    sans = []
    for n in names:
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(n)))
        except ValueError:
            sans.append(x509.DNSName(n))
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder().subject_name(_name(names[0])).issuer_name(ca.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(start).not_valid_after(end)
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    (out / "ca.crt").write_bytes(ca.public_bytes(pem))
    (out / "cert.pem").write_bytes(cert.public_bytes(pem) + ca.public_bytes(pem))
    (out / "key.pem").write_bytes(key.private_bytes(pem, serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption()))
    (out / "key.pem").chmod(0o644)  # read by proxies running as other users in their containers; a run's own


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit("usage: python -m e2e.certs DIR NAME...")
    make(Path(sys.argv[1]), sys.argv[2:])
