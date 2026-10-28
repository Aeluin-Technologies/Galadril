"""Bootstrap contracts protect persistent credentials and disposable development trust."""

from __future__ import annotations

import asyncio
import json
import shutil
import stat
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import jwt
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import SecretStr

from infrastructure.dev.galadril_dev.bootstrap import Bootstrap, BootstrapError
from infrastructure.dev.galadril_dev.identity import certificates

SOURCE = Path(__file__).resolve().parents[2]


class BootstrapTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        for relative in (
            "infrastructure/docker/.env.example",
            "infrastructure/docker/connectors.yaml",
            "infrastructure/envoy/local.yaml",
        ):
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SOURCE / relative, destination)
        self.bootstrap = Bootstrap(self.root)

    def tearDown(self) -> None:
        self.directory.cleanup()

    async def test_proxy_uses_file_owner_for_private_certificate_mounts(
        self,
    ) -> None:
        with (
            patch("os.getuid", return_value=1234),
            patch("os.getgid", return_value=5678),
        ):
            await self.bootstrap.initialize()
        environment = self.bootstrap.environment()
        self.assertEqual(environment["GALADRIL_PROXY_UID"], "1234")
        self.assertEqual(environment["GALADRIL_PROXY_GID"], "5678")

    async def test_fresh_clone_creates_private_state_without_static_templates(
        self,
    ) -> None:
        await self.bootstrap.initialize(
            openai_key=SecretStr("synthetic-provider-key")
        )
        state = self.root / ".local/dev"
        self.assertTrue((state / "compose.env").is_file())
        self.assertEqual(
            stat.S_IMODE((state / "compose.env").stat().st_mode), 0o600
        )
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
        env = self.bootstrap.environment()
        self.assertEqual(env["SCRIBE_OPENAI_API_KEY"], "synthetic-provider-key")
        models = json.loads(env["SCRIBE_MODELS"])
        self.assertEqual(
            models[env["SCRIBE_DEFAULT_MODEL"]]["provider"], "openai"
        )
        self.assertNotEqual(env["MINIO_ROOT_PASSWORD"], "minioadmin")
        self.assertNotEqual(env["POSTGRES_PASSWORD"], "postgres")
        connectors = yaml.safe_load((state / "connectors.yaml").read_text())[
            "connectors"
        ]
        self.assertEqual(
            connectors["s3"]["secret_key"], env["MINIO_ROOT_PASSWORD"]
        )
        self.assertEqual(
            connectors["postgres"]["password"], env["GALADRIL_APP_PASSWORD"]
        )
        self.assertEqual(
            connectors["spicedb"]["token"], env["SPICEDB_GRPC_PRESHARED_KEY"]
        )
        self.assertFalse((state / "templates").exists())

    async def test_reinitialization_preserves_keys_credentials_and_provider_key(
        self,
    ) -> None:
        await self.bootstrap.initialize(
            openai_key=SecretStr("synthetic-provider-key")
        )
        env = self.bootstrap.environment()
        key = (self.root / ".local/dev/identity/gateway/key.pem").read_bytes()
        signer = (self.root / ".local/dev/jwt/signing-key.pem").read_bytes()
        await self.bootstrap.initialize()
        self.assertEqual(self.bootstrap.environment(), env)
        self.assertEqual(
            (self.root / ".local/dev/identity/gateway/key.pem").read_bytes(),
            key,
        )
        self.assertEqual(
            (self.root / ".local/dev/jwt/signing-key.pem").read_bytes(), signer
        )

    async def test_provider_selection_updates_generated_defaults_and_preserves_custom_models(
        self,
    ) -> None:
        await self.bootstrap.initialize()
        local = self.bootstrap.environment()
        self.assertEqual(local["SCRIBE_DEFAULT_MODEL"], "")
        self.assertEqual(json.loads(local["SCRIBE_MODELS"]), {})
        await self.bootstrap.initialize(
            openai_key=SecretStr("synthetic-provider-key")
        )
        cloud = self.bootstrap.environment()
        self.assertEqual(
            json.loads(cloud["SCRIBE_MODELS"])[cloud["SCRIBE_DEFAULT_MODEL"]][
                "provider"
            ],
            "openai",
        )
        custom = '{"custom":{"model":"custom","base_url":"http://model/v1"}}'
        self.bootstrap.update_environment(
            {"SCRIBE_MODELS": custom, "SCRIBE_DEFAULT_MODEL": "custom"}
        )
        await self.bootstrap.initialize(
            openai_key=SecretStr("synthetic-replacement-key")
        )
        self.assertEqual(self.bootstrap.environment()["SCRIBE_MODELS"], custom)
        self.assertEqual(
            self.bootstrap.environment()["SCRIBE_DEFAULT_MODEL"], "custom"
        )

    async def test_concurrent_initializations_do_not_rotate_or_mix_identities(
        self,
    ) -> None:
        await asyncio.gather(*(self.bootstrap.initialize() for _ in range(6)))
        state = self.root / ".local/dev/identity"
        ca = x509.load_pem_x509_certificate((state / "ca.pem").read_bytes())
        for name in ("gateway", "registry", "document-compiler"):
            certificate = x509.load_pem_x509_certificate(
                (state / name / "cert.pem").read_bytes()
            )
            certificate.verify_directly_issued_by(ca)

    async def test_certificate_has_loopback_sans_workload_identity_and_valid_chain(
        self,
    ) -> None:
        await self.bootstrap.initialize()
        state = self.root / ".local/dev/identity"
        ca = x509.load_pem_x509_certificate((state / "ca.pem").read_bytes())
        leaf = x509.load_pem_x509_certificate(
            (state / "gateway/cert.pem").read_bytes()
        )
        leaf.verify_directly_issued_by(ca)
        san = leaf.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
        self.assertIn("localhost", san.get_values_for_type(x509.DNSName))
        self.assertIn(
            "spiffe://galadril/gateway",
            san.get_values_for_type(x509.UniformResourceIdentifier),
        )
        self.assertGreater(leaf.not_valid_after_utc, datetime.now(UTC))
        key = serialization.load_pem_private_key(
            (state / "gateway/key.pem").read_bytes(), password=None
        )
        self.assertIsInstance(key, ec.EllipticCurvePrivateKey)
        self.assertEqual(
            key.public_key().public_numbers(),
            leaf.public_key().public_numbers(),
        )

    async def test_token_is_tenant_scoped_short_lived_and_signer_never_mounted(
        self,
    ) -> None:
        await self.bootstrap.initialize()
        token_path = self.bootstrap.token("existing-user", "existing-tenant")
        state = self.root / ".local/dev"
        jwks = json.loads((state / "jwt/jwks.json").read_text())
        self.assertNotIn("d", jwks["keys"][0])
        key = jwt.PyJWK.from_dict(jwks["keys"][0]).key
        claims = jwt.decode(
            token_path.read_text().strip(),
            key,
            algorithms=["ES256"],
            issuer="https://aeluin.gravitalia.com",
            audience="galadril",
        )
        self.assertEqual(claims["tenant_id"], "existing-tenant")
        self.assertEqual(claims["sub"], "existing-user")
        self.assertEqual(claims["exp"] - claims["iat"], 900)
        self.assertNotIn("role", claims)
        self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)
        compose = (state / "compose.yaml").read_text()
        self.assertNotIn("signing-key.pem", compose)
        self.assertNotIn("token.jwt", compose)
        envoy = (state / "envoy.yaml").read_text()
        self.assertIn("local_development", envoy)
        self.assertIn("remote_jwks", envoy)
        self.assertIn("egress_document_compiler", envoy)

    async def test_existing_development_ca_is_adopted_without_replacing_trust(
        self,
    ) -> None:
        legacy = self.root / ".local/identity"
        certificates(legacy)
        ca, key = (
            (legacy / "ca.pem").read_bytes(),
            (legacy / "ca-key.pem").read_bytes(),
        )
        await self.bootstrap.initialize()
        current = self.root / ".local/dev/identity"
        self.assertEqual((current / "ca.pem").read_bytes(), ca)
        self.assertEqual((current / "ca-key.pem").read_bytes(), key)

    async def test_tls_revision_changes_on_renewal_and_restarts_consumers(
        self,
    ) -> None:
        await self.bootstrap.initialize()
        first = self.bootstrap.environment()["GALADRIL_TLS_REVISION"]
        certificates(self.root / ".local/dev/identity", renew=True)
        await self.bootstrap.initialize()
        self.assertNotEqual(
            self.bootstrap.environment()["GALADRIL_TLS_REVISION"], first
        )
        self.assertIn(
            "GALADRIL_TLS_REVISION",
            (self.root / ".local/dev/compose.yaml").read_text(),
        )

    async def test_import_uses_legacy_durable_defaults_instead_of_rotating_volumes(
        self,
    ) -> None:
        source = self.root / "legacy.env"
        source.write_text("AUTH_SESSION_SECRET=existing-session\n")
        await self.bootstrap.initialize(import_env=source)
        values = self.bootstrap.environment()
        self.assertEqual(values["POSTGRES_PASSWORD"], "postgres")
        self.assertEqual(values["MINIO_ROOT_PASSWORD"], "minioadmin")
        self.assertEqual(values["GALADRIL_APP_PASSWORD"], "galadril_app")

    async def test_envoy_any_requirement_uses_the_protobuf_object_shape(
        self,
    ) -> None:
        await self.bootstrap.initialize()
        source = yaml.safe_load(
            (self.root / ".local/dev/envoy.yaml").read_text()
        )
        matched = False
        for listener in source["static_resources"]["listeners"]:
            for chain in listener["filter_chains"]:
                for network_filter in chain["filters"]:
                    for item in network_filter["typed_config"].get(
                        "http_filters", []
                    ):
                        if item["name"] == "envoy.filters.http.jwt_authn":
                            for rule in item["typed_config"]["rules"]:
                                alternative = rule["requires"]["requires_any"]
                                self.assertIsInstance(alternative, dict)
                                self.assertEqual(
                                    len(alternative["requirements"]), 2
                                )
                                matched = True
        self.assertTrue(matched)

    async def test_explicit_import_updates_credentials_without_rotating_managed_keys(
        self,
    ) -> None:
        await self.bootstrap.initialize()
        original = self.bootstrap.environment()[
            "DOCUMENT_COMPILER_S3_SECRET_KEY"
        ]
        source = self.root / "adopt.env"
        source.write_text("POSTGRES_PASSWORD=existing-database-password\n")
        await self.bootstrap.initialize(import_env=source)
        self.assertEqual(
            self.bootstrap.environment()["POSTGRES_PASSWORD"],
            "existing-database-password",
        )
        self.assertEqual(
            self.bootstrap.environment()["DOCUMENT_COMPILER_S3_SECRET_KEY"],
            original,
        )

    async def test_imported_credentials_are_preserved_without_executing_dotenv(
        self,
    ) -> None:
        imported = self.root / "existing.env"
        marker = self.root / "must-not-exist"
        imported.write_text(
            f'MINIO_ROOT_PASSWORD="$(touch {marker})"\nPOSTGRES_PASSWORD=existing-db\nAUTH_SESSION_SECRET=existing-session\n'
        )
        await self.bootstrap.initialize(import_env=imported)
        env = self.bootstrap.environment()
        self.assertEqual(env["POSTGRES_PASSWORD"], "existing-db")
        self.assertEqual(env["AUTH_SESSION_SECRET"], "existing-session")
        self.assertEqual(env["MINIO_ROOT_PASSWORD"], f"$(touch {marker})")
        self.assertFalse(marker.exists())

    async def test_symlinked_state_is_rejected_without_writing_outside_repo(
        self,
    ) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / ".local").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(BootstrapError):
            await self.bootstrap.initialize()
        self.assertEqual(list(outside.iterdir()), [])

    async def test_symlinked_private_file_is_never_replaced(self) -> None:
        await self.bootstrap.initialize()
        target = self.root / "unrelated"
        target.write_text("preserve")
        env = self.root / ".local/dev/compose.env"
        env.unlink()
        env.symlink_to(target)
        with self.assertRaises(BootstrapError):
            await self.bootstrap.initialize()
        self.assertEqual(target.read_text(), "preserve")

    async def test_cli_compose_arguments_use_managed_state_and_compiler_profile(
        self,
    ) -> None:
        await self.bootstrap.initialize()
        args = self.bootstrap.compose_arguments()
        self.assertIn("--env-file", args)
        self.assertIn(str(self.root / ".local/dev/compose.env"), args)
        self.assertIn(
            str(self.root / "infrastructure/docker/document-compiler.yaml"),
            args,
        )
        self.assertIn("document-compiler", args)
        self.assertNotIn(".local/nuxt-dev-envoy.yaml", args)
        self.assertNotIn("--insecure", args)
        self.assertNotIn("--volumes", args)


if __name__ == "__main__":
    unittest.main()
