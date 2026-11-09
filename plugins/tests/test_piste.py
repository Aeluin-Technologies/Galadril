"""Validate authentication, routing and citation provenance without live secrets."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from galadril_mcp_service.providers.piste_api import (
    PisteClient,
    PisteCredentials,
    PisteUnavailable,
    read_private_credential,
)
from galadril_mcp_service.providers.piste_references import (
    Citation,
    verify_reference,
)
from pydantic import SecretStr


class PisteTest(unittest.IsolatedAsyncioTestCase):
    def test_oauth_credentials_work_without_a_judilibre_api_key_file(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            oauth = Path(temporary) / "oauth"
            oauth.write_text(
                "CLIENT_KEY=fixture-client\nSECRET_KEY=fixture-secret\n"
            )
            oauth.chmod(0o600)
            with patch.dict(
                os.environ, {"LAW_PISTE_OAUTH_FILE": str(oauth)}, clear=True
            ):
                credentials = PisteCredentials.load()
            self.assertIsNone(credentials.judilibre_key)
            self.assertIsNotNone(credentials.client_id)
            self.assertEqual(
                credentials.client_id.get_secret_value(), "fixture-client"
            )

    async def test_empty_or_out_of_bounds_search_is_rejected_without_io(
        self,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.fail("Invalid queries must be rejected before authentication")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http:
            client = PisteClient(http, PisteCredentials())
            for query, page in (
                ("", 1),
                ("x" * 513, 1),
                ("jury", 0),
                ("jury", 101),
            ):
                with self.subTest(page=page), self.assertRaises(ValueError):
                    await client.search("legifrance", query, page=page)

    def test_json_secrets_and_unsafe_secret_files(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaises(PisteUnavailable),
        ):
            PisteCredentials.load()
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "credentials"
            source.write_text(
                json.dumps(
                    {"client_id": "fixture", "client_secret": "fixture-secret"}
                )
            )
            source.chmod(0o600)
            with patch.dict(
                os.environ,
                {"LAW_PISTE_CREDENTIALS_FILE": str(source)},
                clear=True,
            ):
                self.assertEqual(
                    PisteCredentials.load().client_id.get_secret_value(),
                    "fixture",
                )
            source.chmod(0o644)
            with self.assertRaises(PisteUnavailable):
                read_private_credential(str(source))
            source.chmod(0o600)
            link = source.with_name("linked")
            link.symlink_to(source)
            with self.assertRaises(PisteUnavailable):
                read_private_credential(str(link))
            fifo = source.with_name("fifo")
            os.mkfifo(fifo, 0o600)
            with self.assertRaises(PisteUnavailable):
                read_private_credential(str(fifo))
            source.write_bytes(b"x" * 8193)
            with self.assertRaises(PisteUnavailable):
                read_private_credential(str(source))
        with self.assertRaises(PisteUnavailable):
            read_private_credential("relative/credentials")

    async def test_official_consultation_uses_each_funds_identifier_field(
        self,
    ) -> None:
        fields: list[tuple[str, dict[str, object]]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "oauth.piste.gouv.fr":
                return httpx.Response(
                    200,
                    json={
                        "access_token": "fixture-token",
                        "expires_in": 3600,
                        "token_type": "Bearer",
                    },
                )
            fields.append(
                (
                    request.url.path.rsplit("/", 1)[-1],
                    json.loads(request.content),
                )
            )
            return httpx.Response(200, json={"text": {}})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http:
            client = PisteClient(
                http,
                PisteCredentials(
                    client_id="fixture", client_secret="fixture-secret"
                ),
            )
            for identifier in (
                "CETATEXT000025041089",
                "LEGIARTI000000000001",
                "JORFTEXT000048167276",
            ):
                await client.decision("legifrance", identifier)
        self.assertEqual(
            fields,
            [
                ("juri", {"textId": "CETATEXT000025041089"}),
                ("getArticle", {"id": "LEGIARTI000000000001"}),
                ("jorf", {"textCid": "JORFTEXT000048167276"}),
            ],
        )

    def test_environment_credentials_do_not_require_secret_files(self) -> None:
        with patch.dict(
            os.environ,
            {
                "LAW_PISTE_API_KEY": "fixture-api",
                "LAW_PISTE_CLIENT_ID": "fixture-client",
                "LAW_PISTE_CLIENT_SECRET": "fixture-oauth",
                "LAW_PISTE_ENVIRONMENT": "sandbox",
            },
            clear=True,
        ):
            credentials = PisteCredentials.load()
        self.assertEqual(credentials.environment, "sandbox")
        self.assertEqual(
            credentials.client_id.get_secret_value(), "fixture-client"
        )
        self.assertEqual(
            credentials.judilibre_key.get_secret_value(), "fixture-api"
        )
        self.assertNotIn("fixture-oauth", repr(credentials))

    async def test_missing_normative_date_is_rejected_before_network_access(
        self,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.fail("Invalid search must not access PISTE")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http:
            client = PisteClient(http, PisteCredentials())
            with self.assertRaisesRegex(ValueError, "date"):
                await client.search(
                    "legifrance", "délégation", fund="CODE_DATE"
                )

    def test_invented_identity_or_passage_cannot_be_marked_verified(
        self,
    ) -> None:
        record = {
            "provider": "legifrance",
            "operation": "consult",
            "id": "CETATEXT000025041089",
            "sha256": "source-hash",
            "data": {
                "text": {
                    "id": "CETATEXT000025041089",
                    "num": "335033",
                    "juridiction": "Conseil d'État",
                    "dateTexte": 1324598400000,
                    "texte": "La procédure constitue une garantie.",
                }
            },
        }
        citation = Citation(
            provider="legifrance",
            identifier="CETATEXT000025041089",
            number="335033",
            court="Conseil d'État",
            date="2011-12-23",
            passage="La procédure constitue une garantie.",
        )
        self.assertTrue(verify_reference(citation, record)["passage_verified"])
        self.assertFalse(
            verify_reference(
                citation.model_copy(update={"number": "999999"}), record
            )["identity_verified"]
        )
        self.assertFalse(
            verify_reference(
                citation.model_copy(
                    update={"passage": "Tous les vices sont sans effet."}
                ),
                record,
            )["passage_verified"]
        )
        self.assertFalse(
            verify_reference(citation, record)["applicability_verified"]
        )
        for passage in ("   ", "<p></p>", "&nbsp;"):
            with self.subTest(passage=passage):
                self.assertFalse(
                    verify_reference(
                        citation.model_copy(update={"passage": passage}), record
                    )["passage_verified"]
                )
        for field, value in (
            ("court", "Cour de cassation"),
            ("date", None),
            ("identifier", "CETATEXT000000000000"),
        ):
            with self.subTest(field=field):
                self.assertFalse(
                    verify_reference(
                        citation.model_copy(update={field: value}), record
                    )["identity_verified"]
                )
        for field, value in (
            ("provider", "judilibre"),
            ("operation", "search"),
            ("id", "other"),
        ):
            with self.subTest(field=field):
                self.assertFalse(
                    verify_reference(citation, record | {field: value})[
                        "identity_verified"
                    ]
                )

    def test_malformed_records_never_gain_quote_or_identity_verification(
        self,
    ) -> None:
        citation = Citation(
            provider="judilibre",
            identifier="synthetic",
            number="20-12345",
            court="Cour de cassation",
            date="2020-01-02",
            passage="Motif synthétique vérifiable.",
        )
        record = {
            "provider": "judilibre",
            "operation": "consult",
            "id": "synthetic",
            "data": {
                "id": "synthetic",
                "number": "20-12345",
                "jurisdiction": "Cour de cassation",
                "decision_date": "2020-01-02",
                "text": citation.passage,
            },
        }
        self.assertTrue(verify_reference(citation, record)["passage_verified"])
        for data in (
            None,
            [],
            "invalid",
            {},
            record["data"] | {"jurisdiction": None},
            record["data"] | {"decision_date": "not-a-date"},
        ):
            result = verify_reference(citation, record | {"data": data})
            self.assertFalse(result["identity_verified"])
            self.assertFalse(result["passage_verified"])

    def test_separate_key_and_oauth_files_remain_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            key = Path(temporary) / "key"
            oauth = Path(temporary) / "oauth"
            key.write_text(
                "API_KEY=fixture-api\nSECRET_KEY=unused-signing-secret\n"
            )
            oauth.write_text(
                "CLIENT_KEY=fixture-client\nSECRET_KEY=fixture-oauth-secret\n"
            )
            key.chmod(0o600)
            oauth.chmod(0o600)
            with patch.dict(
                os.environ,
                {
                    "LAW_PISTE_CREDENTIALS_FILE": str(key),
                    "LAW_PISTE_OAUTH_FILE": str(oauth),
                },
            ):
                credentials = PisteCredentials.load()
            self.assertEqual(
                credentials.client_id.get_secret_value(), "fixture-client"
            )
            self.assertEqual(
                credentials.judilibre_key.get_secret_value(), "fixture-api"
            )
            self.assertNotIn("fixture-oauth-secret", repr(credentials))

    async def test_judilibre_key_is_only_sent_to_the_fixed_origin(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(
                200,
                json={
                    "id": "synthetic",
                    "text": "Motif vérifiable",
                    "number": "20-12345",
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http:
            client = PisteClient(
                http, PisteCredentials(judilibre_key="fixture-secret")
            )
            answer = await client.decision("judilibre", "synthetic")
            self.assertEqual(calls[0].headers["KeyId"], "fixture-secret")
            self.assertEqual(calls[0].url.host, "api.piste.gouv.fr")
            self.assertNotIn("fixture-secret", str(answer))
            self.assertIn("sha256", answer)
            search = await client.search("judilibre", "delegation", page=2)
            self.assertEqual(calls[-1].url.params["page"], "1")
            self.assertFalse(search["citation_verified"])
            with self.assertRaises(ValueError):
                await client.decision("legifrance", "https://attacker.invalid/")

    def test_unavailable_judilibre_file_does_not_disable_legifrance(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "piste-key"
            with patch.dict(
                os.environ,
                {
                    "LAW_PISTE_CREDENTIALS_FILE": str(path),
                    "LAW_PISTE_CLIENT_ID": "fixture-id",
                    "LAW_PISTE_CLIENT_SECRET": "fixture-secret",
                },
                clear=True,
            ):
                for content in (None, "SECRET_KEY=unused\n", "API_KEY=\n"):
                    if content is not None:
                        path.write_text(content)
                        path.chmod(0o600)
                    credentials = PisteCredentials.load()
                    self.assertIsNone(credentials.judilibre_key)
                    self.assertEqual(
                        credentials.client_id, SecretStr("fixture-id")
                    )

    async def test_oauth_token_is_reused_and_search_is_not_a_verified_citation(
        self,
    ) -> None:
        token_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal token_calls
            if request.url.host == "oauth.piste.gouv.fr":
                token_calls += 1
                return httpx.Response(
                    200,
                    json={
                        "access_token": "fixture-token",
                        "expires_in": 3600,
                        "token_type": "Bearer",
                    },
                )
            self.assertEqual(
                request.headers["authorization"], "Bearer fixture-token"
            )
            return httpx.Response(200, json={"results": []})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http:
            client = PisteClient(
                http,
                PisteCredentials(
                    client_id="fixture", client_secret="fixture-secret"
                ),
            )
            for _ in range(2):
                answer = await client.search(
                    "legifrance", "délégation", fund="CETAT"
                )
                self.assertFalse(answer["citation_verified"])
            self.assertEqual(token_calls, 1)


if __name__ == "__main__":
    unittest.main()
