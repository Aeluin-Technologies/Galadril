"""Stable development CA keys permit leaf renewal without invalidating trust."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .files import BootstrapError, private_directory, read, write

WORKLOADS = (
    "gateway",
    "registry",
    "intake",
    "vision",
    "scribe",
    "hydra",
    "document-compiler",
)


def _key(path: Path) -> ec.EllipticCurvePrivateKey:
    if not path.exists() and not path.is_symlink():
        key = ec.generate_private_key(ec.SECP256R1())
        write(
            path,
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )
        return key
    key = serialization.load_pem_private_key(read(path), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise BootstrapError("Expected an EC development signing key")
    return key


def certificates(directory: Path, *, renew: bool = False) -> None:
    """Renew leaves before expiry while keeping private CA and workload keys."""
    private_directory(directory)
    now = datetime.now(UTC)
    ca_key = _key(directory / "ca-key.pem")
    ca_path = directory / "ca.pem"
    ca = (
        x509.load_pem_x509_certificate(read(ca_path))
        if ca_path.exists()
        else None
    )
    if ca is not None and ca.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ) != ca_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ):
        raise BootstrapError(
            "Development CA certificate does not match its key"
        )
    if ca is None or ca.not_valid_after_utc <= now + timedelta(days=7):
        subject = x509.Name(
            [
                x509.NameAttribute(
                    NameOID.COMMON_NAME, "Galadril development workload CA"
                )
            ]
        )
        ca = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=365))
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=0), critical=True
            )
            .add_extension(
                x509.KeyUsage(
                    False, False, False, False, False, True, True, False, False
                ),
                critical=True,
            )
            .sign(ca_key, hashes.SHA256())
        )
        write(ca_path, ca.public_bytes(serialization.Encoding.PEM), public=True)
        renew = True
    for name in WORKLOADS:
        leaf_dir = directory / name
        private_directory(leaf_dir)
        key = _key(leaf_dir / "key.pem")
        leaf_path = leaf_dir / "cert.pem"
        leaf = (
            x509.load_pem_x509_certificate(read(leaf_path))
            if leaf_path.exists()
            else None
        )
        if leaf is not None:
            try:
                leaf.verify_directly_issued_by(ca)
            except (ValueError, TypeError):
                renew = True
            if leaf.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ) != key.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ):
                raise BootstrapError(
                    "Workload certificate does not match its key"
                )
        if (
            renew
            or leaf is None
            or leaf.not_valid_after_utc <= now + timedelta(days=7)
        ):
            subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
            leaf = (
                x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(ca.subject)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5))
                .not_valid_after(now + timedelta(days=30))
                .add_extension(
                    x509.BasicConstraints(ca=False, path_length=None),
                    critical=True,
                )
                .add_extension(
                    x509.SubjectAlternativeName(
                        [
                            x509.UniformResourceIdentifier(
                                f"spiffe://galadril/{name}"
                            ),
                            x509.DNSName(f"{name}-proxy"),
                            x509.DNSName("localhost"),
                            x509.IPAddress(ip_address("127.0.0.1")),
                            x509.IPAddress(ip_address("::1")),
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
                .add_extension(
                    x509.KeyUsage(
                        True,
                        False,
                        False,
                        False,
                        False,
                        False,
                        False,
                        False,
                        False,
                    ),
                    critical=True,
                )
                .sign(ca_key, hashes.SHA256())
            )
            write(
                leaf_path,
                leaf.public_bytes(serialization.Encoding.PEM),
                public=True,
            )
        write(
            leaf_dir / "ca.pem",
            ca.public_bytes(serialization.Encoding.PEM),
            public=True,
        )
