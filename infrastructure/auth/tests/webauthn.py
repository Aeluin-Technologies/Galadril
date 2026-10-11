"""Software authenticator confined to isolated integration test identities."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Mapping
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fido2.cose import ES256
from fido2.utils import websafe_encode
from fido2.webauthn import (
    Aaguid,
    AttestationObject,
    AttestedCredentialData,
    AuthenticatorData,
    CollectedClientData,
)


class VirtualAuthenticator:
    """Signs WebAuthn proofs using a test-only ES256 credential and upstream codecs."""

    __slots__ = ("_key", "_id", "_counter")

    def __init__(self) -> None:
        self._key = ec.generate_private_key(ec.SECP256R1())
        self._id = secrets.token_bytes(32)
        self._counter = 0

    def respond(
        self, step: Mapping[str, object], origin: str, *, verified: bool = True
    ) -> dict[str, object]:
        """Creates enrollment or assertion data bound to one origin and challenge."""
        options = step["options"]
        if not isinstance(options, dict):
            raise TypeError("Expected authenticator options")
        registration = step["operation"] == "registration"
        client = CollectedClientData.create(
            "webauthn.create" if registration else "webauthn.get",
            str(options["challenge"]),
            origin,
        )
        flags = AuthenticatorData.FLAG.UP
        if verified:
            flags |= AuthenticatorData.FLAG.UV
        credential = b""
        if registration:
            flags |= AuthenticatorData.FLAG.AT
            credential = AttestedCredentialData.create(
                Aaguid.NONE,
                self._id,
                ES256.from_cryptography_key(self._key.public_key()),
            )
        else:
            self._counter += 1
        data = AuthenticatorData.create(
            hashlib.sha256((urlsplit(origin).hostname or "").encode()).digest(),
            flags,
            0 if registration else self._counter,
            credential,
        )
        response: dict[str, object] = {"clientDataJSON": websafe_encode(client)}
        if registration:
            response.update(
                attestationObject=websafe_encode(
                    AttestationObject.create("none", data, {})
                ),
                transports=["internal"],
            )
        else:
            response.update(
                authenticatorData=websafe_encode(data),
                signature=websafe_encode(
                    self._key.sign(
                        bytes(data) + client.hash, ec.ECDSA(hashes.SHA256())
                    )
                ),
                userHandle=None,
            )
        return {
            "id": websafe_encode(self._id),
            "rawId": websafe_encode(self._id),
            "type": "public-key",
            "clientExtensionResults": {},
            "response": response,
        }
