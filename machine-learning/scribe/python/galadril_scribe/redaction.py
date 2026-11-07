"""Private native processing has separate capacity and no model or tool authority."""

import asyncio
import json
import os
import sys

import structlog
from opentelemetry import metrics, trace

from galadril_scribe.redaction_contracts import (
    MAX_WIRE_BYTES,
    RedactionInput,
    RedactionResult,
)

_OUTCOMES = metrics.get_meter("galadril.scribe.redaction").create_counter(
    "galadril.redaction.operations"
)


class RedactionFailure(RuntimeError):
    __slots__ = ()


class RedactionCapacity(RedactionFailure):
    __slots__ = ()


class RedactionRuntime:
    __slots__ = ("active",)

    def __init__(self) -> None:
        self.active = 0

    async def run(self, request: RedactionInput) -> RedactionResult:
        payload = json.dumps(
            {
                "mime": request.mime,
                "content_base64": request.content_base64.get_secret_value(),
                "rules": [
                    {
                        "kind": rule.kind,
                        "pattern": rule.pattern.get_secret_value()
                        if rule.pattern is not None
                        else None,
                        "replacement": rule.replacement,
                        "case_sensitive": rule.case_sensitive,
                        "replacement_mode": rule.replacement_mode,
                    }
                    for rule in request.rules
                ],
            }
        ).encode()
        return RedactionResult.model_validate_json(
            await self.run_bytes(payload)
        )

    async def run_bytes(self, payload: bytes) -> bytes:
        """Parse private JSON only in the bounded child, keeping the HTTP loop responsive."""
        if self.active >= 2:
            raise RedactionCapacity("Private processing capacity exceeded")
        self.active += 1
        process: asyncio.subprocess.Process | None = None
        try:
            if len(payload) > MAX_WIRE_BYTES:
                raise RedactionFailure("Private input exceeds limit")
            with trace.get_tracer("galadril.scribe").start_as_current_span(
                "document.redact",
                record_exception=False,
                set_status_on_exception=False,
            ):
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "galadril_scribe.redaction_worker",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env={
                        "PATH": os.defpath,
                        "PYTHONPATH": os.pathsep.join(sys.path),
                        "LANG": "C.UTF-8",
                        "PYTHONDONTWRITEBYTECODE": "1",
                    },
                )
                if process.stdin is None or process.stdout is None:
                    raise RedactionFailure("Private processing unavailable")

                async def send() -> None:
                    if process is None or process.stdin is None:
                        raise RedactionFailure("Private processing unavailable")
                    process.stdin.write(payload)
                    await process.stdin.drain()
                    process.stdin.close()
                    await process.stdin.wait_closed()

                async def receive() -> bytearray:
                    result = bytearray()
                    if process is None or process.stdout is None:
                        raise RedactionFailure("Private processing unavailable")
                    while chunk := await process.stdout.read(65536):
                        if len(result) + len(chunk) > MAX_WIRE_BYTES:
                            raise RedactionFailure(
                                "Private output exceeds limit"
                            )
                        result.extend(chunk)
                    return result

                async with asyncio.timeout(20):
                    async with asyncio.TaskGroup() as group:
                        group.create_task(send())
                        reader = group.create_task(receive())
                    status = await process.wait()
                    output = reader.result()
                if status != 0:
                    raise RedactionFailure("Document redaction refused")
                _OUTCOMES.add(1, {"outcome": "success"})
                return bytes(output)
        except asyncio.CancelledError:
            _OUTCOMES.add(1, {"outcome": "cancelled"})
            raise
        except Exception as error:
            _OUTCOMES.add(1, {"outcome": "refused"})
            structlog.get_logger().warning(
                "redaction.refused", error_type=type(error).__name__
            )
            raise RedactionFailure("Document redaction refused") from None
        finally:
            try:
                if process is not None and process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        # The child may exit between checking returncode and killing it.
                        pass
                    await asyncio.shield(process.wait())
            finally:
                self.active -= 1
