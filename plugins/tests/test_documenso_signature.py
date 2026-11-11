"""Exercise Documenso V2 without delivering documents or invitations."""

import hashlib
import json
import unittest
from email import policy
from email.parser import BytesParser
from uuid import UUID

import httpx
from galadril_plugins.documenso import Documenso
from galadril_plugins.signatures import (
    EnvelopeDraft,
    Placement,
    ProviderError,
    SignatureKind,
    Signer,
)

_ID = "envelope_abcdefghijklmnop"
_PDF = b"%PDF-1.7\nfixture"
_REQUEST = UUID("0fdbb2a5-a4cc-4aeb-8328-c34d762cce4c")


def draft(kind: SignatureKind = SignatureKind.CRYPTOGRAPHIC) -> EnvelopeDraft:
    return EnvelopeDraft(
        request_id=_REQUEST,
        title="Agreement",
        original_sha256=hashlib.sha256(_PDF).hexdigest(),
        kind=kind,
        signers=(
            Signer(
                name="One",
                email="one@example.test",
                placement=Placement(
                    name="claimant", page=1, x=60, y=80, width=20, height=7
                ),
            ),
            Signer(
                name="Two",
                email="two@example.test",
                placement=Placement(
                    name="counsel", page=2, x=10, y=80, width=20, height=7
                ),
            ),
        ),
    )


class DocumensoTest(unittest.IsolatedAsyncioTestCase):
    async def test_draft_binds_digest_and_order_before_explicit_distribution(
        self,
    ) -> None:
        calls: list[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            self.assertEqual(request.headers["authorization"], "api_example")
            if request.url.path.endswith("/create"):
                message = BytesParser(policy=policy.default).parsebytes(
                    b"Content-Type: "
                    + request.headers["content-type"].encode()
                    + b"\r\n\r\n"
                    + request.read()
                )
                body = message.get_body()
                assert body is not None
                payload = json.loads(body.get_content())
                self.assertEqual(payload["externalId"], str(_REQUEST))
                self.assertEqual(payload["meta"]["signingOrder"], "SEQUENTIAL")
                self.assertEqual(
                    [s["signingOrder"] for s in payload["recipients"]], [1, 2]
                )
                self.assertEqual(
                    [s["fields"][0]["page"] for s in payload["recipients"]],
                    [1, 2],
                )
                return httpx.Response(200, json={"id": _ID})
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "id": _ID,
                        "status": "DRAFT",
                        "externalId": str(_REQUEST),
                        "signatureLevel": "AES",
                        "recipients": [
                            {
                                "email": "one@example.test",
                                "role": "SIGNER",
                                "signingOrder": 1,
                            },
                            {
                                "email": "two@example.test",
                                "role": "SIGNER",
                                "signingOrder": 2,
                            },
                        ],
                        "documentMeta": {"signingOrder": "SEQUENTIAL"},
                    },
                )
            self.assertEqual(json.loads(request.read()), {"envelopeId": _ID})
            return httpx.Response(200, json={"success": True, "id": _ID})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle)
        ) as client:
            provider = Documenso(
                client,
                "https://documenso.example/api/v2",
                "api_example",
                "a" * 32,
            )
            with self.assertRaises(ProviderError):
                await provider.create_draft(draft(), _PDF + b"modified")
            self.assertEqual(calls, [])
            ref = await provider.create_draft(draft(), _PDF)
            self.assertEqual(len(calls), 1)
            with self.assertRaises(ProviderError):
                await provider.distribute(
                    ref, draft(SignatureKind.QUALIFIED_REQUESTED)
                )
            await provider.distribute(ref, draft())
            self.assertEqual(calls[-1], "/api/v2/envelope/distribute")

    async def test_unconfirmed_or_altered_draft_cannot_be_distributed(
        self,
    ) -> None:
        for change in (
            {"signatureLevel": None},
            {
                "recipients": [
                    {
                        "email": "other@example.test",
                        "role": "SIGNER",
                        "signingOrder": 1,
                    }
                ]
            },
        ):

            def handle(
                request: httpx.Request, change: dict[str, object] = change
            ) -> httpx.Response:
                self.assertEqual(request.method, "GET")
                return httpx.Response(
                    200,
                    json={
                        "id": _ID,
                        "status": "DRAFT",
                        "externalId": str(_REQUEST),
                        "signatureLevel": "AES",
                        "documentMeta": {"signingOrder": "SEQUENTIAL"},
                        "recipients": [
                            {
                                "email": "one@example.test",
                                "role": "SIGNER",
                                "signingOrder": 1,
                            },
                            {
                                "email": "two@example.test",
                                "role": "SIGNER",
                                "signingOrder": 2,
                            },
                        ],
                        **change,
                    },
                )

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handle)
            ) as client:
                provider = Documenso(
                    client,
                    "https://documenso.example/api/v2",
                    "api_example",
                    "a" * 32,
                )
                with self.assertRaises(ProviderError):
                    await provider.distribute(_ID, draft())

    async def test_redirect_and_ambiguous_mutation_are_never_retried(
        self,
    ) -> None:
        for status in (302, 503):
            calls = 0

            def handle(
                request: httpx.Request, status: int = status
            ) -> httpx.Response:
                nonlocal calls
                calls += 1
                return httpx.Response(
                    status, headers={"location": "https://other.example/"}
                )

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handle), follow_redirects=True
            ) as client:
                provider = Documenso(
                    client,
                    "https://documenso.example/api/v2",
                    "api_example",
                    "a" * 32,
                )
                with self.assertRaises(ProviderError) as caught:
                    await provider.create_draft(draft(), _PDF)
                self.assertTrue(caught.exception.reconcile_before_retry)
                self.assertEqual(calls, 1)

    async def test_webhook_hint_cannot_complete_an_envelope(self) -> None:
        status = "PENDING"
        download_count = 0

        def handle(request: httpx.Request) -> httpx.Response:
            nonlocal download_count
            if request.url.path.endswith("/download"):
                download_count += 1
                self.assertEqual(request.url.params["version"], "signed")
                return httpx.Response(200, content=_PDF + b"provider revision")
            return httpx.Response(
                200,
                json={
                    "id": _ID,
                    "status": status,
                    "externalId": str(_REQUEST),
                    "signatureLevel": "AES",
                    "documentMeta": {"signingOrder": "SEQUENTIAL"},
                    "recipients": [
                        {
                            "email": "one@example.test",
                            "role": "SIGNER",
                            "signingOrder": 1,
                            "signingStatus": "SIGNED",
                        },
                        {
                            "email": "two@example.test",
                            "role": "SIGNER",
                            "signingOrder": 2,
                            "signingStatus": "SIGNED"
                            if status == "COMPLETED"
                            else "NOT_SIGNED",
                        },
                    ],
                    "envelopeItems": [{"id": "envelope_item_abcdefghijklmnop"}],
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle)
        ) as client:
            provider = Documenso(
                client,
                "https://documenso.example/api/v2",
                "api_example",
                "a" * 32,
            )
            hint = provider.webhook_hint(
                b"a" * 32,
                json.dumps(
                    {
                        "event": "DOCUMENT_COMPLETED",
                        "payload": {"envelopeId": _ID},
                    }
                ).encode(),
            )
            self.assertEqual(
                (await provider.status(hint.envelope_id, draft())).value,
                "pending",
            )
            with self.assertRaises(ProviderError):
                await provider.download_completed(hint.envelope_id, draft())
            self.assertEqual(download_count, 0)
            status = "COMPLETED"
            artifact = await provider.download_completed(
                hint.envelope_id, draft()
            )
            self.assertEqual(artifact.pdf, _PDF + b"provider revision")
            self.assertEqual(
                artifact.sha256, hashlib.sha256(artifact.pdf).hexdigest()
            )

    async def test_webhook_secret_is_mandatory_and_hint_is_replay_stable(
        self,
    ) -> None:
        body = json.dumps(
            {
                "event": "DOCUMENT_COMPLETED",
                "payload": {"envelopeId": _ID, "token": "ignored"},
            }
        ).encode()
        async with httpx.AsyncClient() as client:
            provider = Documenso(
                client,
                "https://documenso.example/api/v2",
                "api_example",
                "a" * 32,
            )
            for secret in (None, b"", b"wrong"):
                with self.assertRaises(ProviderError):
                    provider.webhook_hint(secret, body)
            hint = provider.webhook_hint(b"a" * 32, body)
            self.assertEqual(hint, provider.webhook_hint(b"a" * 32, body))
            self.assertEqual(hint.envelope_id, _ID)
            self.assertNotIn("ignored", repr(hint))


if __name__ == "__main__":
    unittest.main()
