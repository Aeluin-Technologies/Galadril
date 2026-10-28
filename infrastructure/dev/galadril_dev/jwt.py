"""Disposable local JWT issuer; signing keys never enter application mounts."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import cast

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

ISSUER = "https://aeluin.gravitalia.com"
AUDIENCE = "galadril"


def initialize(directory: Path) -> None:
    """Creates owner-only signing material without replacing existing keys."""
    directory.mkdir(mode=0o700, parents=True)
    key = ec.generate_private_key(ec.SECP256R1())
    public = cast(
        dict[str, object], json.loads(ECAlgorithm.to_jwk(key.public_key()))
    )
    public.update(alg="ES256", use="sig", kid=_key_id(key))
    descriptor = os.open(
        directory / "signing-key.pem",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "wb") as output:
        output.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    with (directory / "jwks.json").open("x", encoding="utf-8") as output:
        json.dump({"keys": [public]}, output)
        output.write("\n")


def _key_id(key: ec.EllipticCurvePrivateKey) -> str:
    return hashlib.sha256(
        key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    ).hexdigest()


def issue_token(directory: Path, subject: str, tenant_id: str) -> str:
    """Bounds local credentials to one tenant and a 15-minute validity window."""
    if not subject.strip() or not tenant_id.strip():
        raise ValueError("Subject and tenant must be nonempty")
    key = serialization.load_pem_private_key(
        (directory / "signing-key.pem").read_bytes(), password=None
    )
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError("Expected an EC signing key")
    now = int(time.time())
    return jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": subject,
            "tenant_id": tenant_id,
            "iat": now,
            "nbf": now,
            "exp": now + 900,
        },
        key,
        algorithm="ES256",
        headers={"kid": _key_id(key)},
    )
