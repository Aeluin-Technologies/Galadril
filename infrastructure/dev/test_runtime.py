"""CLI process boundaries must preserve errors without leaking credentials."""

from __future__ import annotations

import asyncio
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

from infrastructure.dev.galadril_dev.runtime import (
    CommandError,
    ContainerStatus,
    pin_compiler_images,
    run,
    trust_command,
    wait_for_compiler,
)


class RuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_cli_output_is_english_even_with_french_locale(self) -> None:
        from rich.console import Console

        from infrastructure.dev.galadril_dev import cli

        output = StringIO()
        bootstrap = unittest.mock.Mock()
        bootstrap.state = Path("/private/state")
        bootstrap.initialize = AsyncMock(
            return_value=unittest.mock.Mock(openai_configured=True)
        )
        args = cli.parser().parse_args(
            ["init", "--no-input"], namespace=cli.Arguments()
        )
        with (
            patch.object(cli, "Bootstrap", return_value=bootstrap),
            patch.dict("os.environ", {"LANG": "fr_FR.UTF-8"}),
        ):
            await cli.execute(args, Console(file=output, width=160))
        self.assertIn("Private configuration ready", output.getvalue())
        self.assertIn("Next command: up", output.getvalue())

    async def test_readiness_uses_health_rpc_instead_of_otlp_log_output(
        self,
    ) -> None:
        bootstrap = unittest.mock.Mock()
        bootstrap.environment.return_value = {}
        bootstrap.compose_arguments.return_value = ("docker", "compose")
        status = ContainerStatus(
            Name="compiler", Service="document-compiler", State="running"
        )
        with (
            patch(
                "infrastructure.dev.galadril_dev.runtime.containers",
                new_callable=AsyncMock,
                return_value=[status],
            ),
            patch(
                "infrastructure.dev.galadril_dev.runtime.run",
                new_callable=AsyncMock,
                return_value="",
            ) as execute,
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await wait_for_compiler(bootstrap)
        self.assertEqual(
            execute.call_args.args[0][-5:],
            (
                "exec",
                "--no-TTY",
                "document-compiler",
                "/document-compiler",
                "--check-ready",
            ),
        )

    async def test_local_content_addressed_images_are_already_immutable(
        self,
    ) -> None:
        bootstrap = unittest.mock.Mock()
        bootstrap.environment.return_value = {
            "GALADRIL_DOCUMENT_COMPILER_IMAGE": "sha256:" + "a" * 64,
            "GALADRIL_DOCUMENT_WORKER_IMAGE": "sha256:" + "b" * 64,
            "DOCUMENT_COMPILER_BUNDLE_SHA256": "c" * 64,
        }
        with patch(
            "infrastructure.dev.galadril_dev.runtime.run",
            new_callable=AsyncMock,
        ) as execute:
            await pin_compiler_images(bootstrap, pull=False)
        execute.assert_not_awaited()
        revision = bootstrap.update_environment.call_args.args[0][
            "DOCUMENT_COMPILER_RUNTIME_REVISION"
        ]
        self.assertRegex(revision, r"^[0-9a-f]{64}$")
        bootstrap.environment.return_value[
            "DOCUMENT_COMPILER_RUNTIME_REVISION"
        ] = revision
        bootstrap.update_environment.reset_mock()
        await pin_compiler_images(bootstrap, pull=False)
        bootstrap.update_environment.assert_not_called()

    async def test_worker_and_bundle_changes_select_separate_public_volumes(
        self,
    ) -> None:
        async def select(worker: str, bundle: str) -> str:
            bootstrap = unittest.mock.Mock()
            bootstrap.environment.return_value = {
                "GALADRIL_DOCUMENT_COMPILER_IMAGE": "sha256:" + "a" * 64,
                "GALADRIL_DOCUMENT_WORKER_IMAGE": worker,
                "DOCUMENT_COMPILER_BUNDLE_SHA256": bundle,
            }
            await pin_compiler_images(bootstrap, pull=False)
            return bootstrap.update_environment.call_args.args[0][
                "DOCUMENT_COMPILER_RUNTIME_REVISION"
            ]

        original = await select("sha256:" + "b" * 64, "c" * 64)
        self.assertEqual(original, await select("sha256:" + "b" * 64, "c" * 64))
        self.assertEqual(
            original,
            await select("registry.test/worker@sha256:" + "b" * 64, "c" * 64),
        )
        self.assertNotEqual(
            original, await select("sha256:" + "d" * 64, "c" * 64)
        )
        self.assertNotEqual(
            original, await select("sha256:" + "b" * 64, "d" * 64)
        )

    async def test_invalid_runtime_provenance_is_not_published(self) -> None:
        bootstrap = unittest.mock.Mock()
        bootstrap.environment.return_value = {
            "GALADRIL_DOCUMENT_COMPILER_IMAGE": "sha256:" + "a" * 64,
            "GALADRIL_DOCUMENT_WORKER_IMAGE": "registry.test/worker@sha256:invalid",
            "DOCUMENT_COMPILER_BUNDLE_SHA256": "c" * 64,
        }
        from infrastructure.dev.galadril_dev.files import BootstrapError

        with self.assertRaises(BootstrapError):
            await pin_compiler_images(bootstrap, pull=False)
        bootstrap.update_environment.assert_not_called()

    async def test_subprocess_errors_redact_secrets_and_keep_exit_status(
        self,
    ) -> None:
        process = AsyncMock()
        process.returncode = 17
        process.communicate.return_value = (
            b"",
            b"synthetic-api-secret was denied",
        )
        with patch.object(
            asyncio, "create_subprocess_exec", return_value=process
        ) as spawn:
            with self.assertRaises(CommandError) as failure:
                await run(
                    ("docker", "info"),
                    environment={
                        "SCRIBE_OPENAI_API_KEY": "synthetic-api-secret"
                    },
                )
            self.assertNotIn("synthetic-api-secret", str(failure.exception))
            self.assertEqual(failure.exception.status, 17)
            self.assertEqual(spawn.call_args.args, ("docker", "info"))
            self.assertNotIn("shell", spawn.call_args.kwargs)

    async def test_timeout_kills_and_reaps_command(self) -> None:
        process = AsyncMock()
        process.kill = unittest.mock.Mock()
        process.communicate.side_effect = [TimeoutError(), (b"", b"")]
        with patch.object(
            asyncio, "create_subprocess_exec", return_value=process
        ):
            with self.assertRaises(CommandError):
                await run(("docker", "info"), environment={}, timeout=0.01)
        process.kill.assert_called_once()
        self.assertEqual(process.communicate.await_count, 2)

    def test_trust_is_explicit_user_scoped_on_macos(self) -> None:
        command = trust_command("Darwin", "/trusted/ca.pem", "/home/test")
        self.assertEqual(command[0], "security")
        self.assertIn("/home/test/Library/Keychains/login.keychain-db", command)
        self.assertNotIn("-d", command)
        self.assertNotIn("/Library/Keychains/System.keychain", command)
        self.assertNotIn("--insecure", command)


if __name__ == "__main__":
    unittest.main()
