"""CLI process boundaries must preserve errors without leaking credentials."""

from __future__ import annotations

import asyncio
import unittest
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
        }
        with patch(
            "infrastructure.dev.galadril_dev.runtime.run",
            new_callable=AsyncMock,
        ) as execute:
            await pin_compiler_images(bootstrap, pull=False)
        execute.assert_not_awaited()
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
