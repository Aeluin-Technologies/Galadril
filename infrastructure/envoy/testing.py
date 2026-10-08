"""Ephemeral PKI fixtures; private CA material never enters deployment bundles."""

from __future__ import annotations

import base64
import ipaddress
import json
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

PUBLIC_KEY = b"""-----BEGIN PUBLIC KEY-----
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEEVs/o5+uQbTjL3chynL4wXgUg2R9
q9UU8I5mEovUf86QZ7kOBIjJwqnzD1omageEHWwHdBO6B+dFabmdT9POxg==
-----END PUBLIC KEY-----
"""


@lru_cache(maxsize=1)
def identity_files() -> dict[str, bytes]:
    """Shares one short-lived trust root between test clients and containers."""
    now = datetime.now(UTC)
    authority_key = ec.generate_private_key(ec.SECP256R1())
    authority_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "E2E CA")]
    )
    authority = (
        x509.CertificateBuilder()
        .subject_name(authority_name)
        .issuer_name(authority_name)
        .public_key(authority_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=0), critical=True
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(
                authority_key.public_key()
            ),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                False, False, False, False, False, True, True, False, False
            ),
            critical=True,
        )
        .sign(authority_key, hashes.SHA256())
    )
    ca = authority.public_bytes(serialization.Encoding.PEM)
    files: dict[str, bytes] = {}
    for name in (
        "gateway",
        "registry",
        "intake",
        "vision",
        "scribe",
        "unknown",
    ):
        key = ec.generate_private_key(ec.SECP256R1())
        certificate = (
            x509.CertificateBuilder()
            .subject_name(
                x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
            )
            .issuer_name(authority_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None), critical=True
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    authority_key.public_key()
                ),
                critical=False,
            )
            .add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.UniformResourceIdentifier(
                            f"spiffe://galadril/{name}"
                        ),
                        x509.DNSName(f"{name}-proxy"),
                        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    ]
                ),
                critical=False,
            )
            .add_extension(
                x509.ExtendedKeyUsage(
                    [
                        ExtendedKeyUsageOID.SERVER_AUTH,
                        ExtendedKeyUsageOID.CLIENT_AUTH,
                    ]
                ),
                critical=False,
            )
            .sign(authority_key, hashes.SHA256())
        )
        prefix = f"identity/{name}"
        files[f"{prefix}/ca.pem"] = ca
        files[f"{prefix}/cert.pem"] = certificate.public_bytes(
            serialization.Encoding.PEM
        )
        files[f"{prefix}/key.pem"] = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    return files


def jwks_bytes() -> bytes:
    """Uses the public half of the existing signed E2E JWT fixture."""
    key = serialization.load_pem_public_key(PUBLIC_KEY)
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise TypeError("Expected an EC test key")
    numbers = key.public_numbers()

    def coordinate(value: int) -> str:
        return (
            base64.urlsafe_b64encode(value.to_bytes(32, "big"))
            .rstrip(b"=")
            .decode("ascii")
        )

    return json.dumps(
        {
            "keys": [
                {
                    "kty": "EC",
                    "crv": "P-256",
                    "alg": "ES256",
                    "use": "sig",
                    "x": coordinate(numbers.x),
                    "y": coordinate(numbers.y),
                }
            ]
        }
    ).encode("ascii")
