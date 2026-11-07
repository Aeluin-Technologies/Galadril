"""Private native processing has separate capacity and no model or tool authority."""

import asyncio
import json
import os
import sys
from pathlib import Path

import structlog
from opentelemetry import metrics, trace

from galadril_scribe.redaction_contracts import (
    MAX_WIRE_BYTES,
    PRIVATE_RESULT,
    PrivateBatch,
    RedactionInput,
    RedactionResult,
)

_OUTCOMES = metrics.get_meter("galadril.scribe.redaction").create_counter(
    "galadril.redaction.operations"
)


def private_model_directory(configured: str) -> Path:
    """Resolve deployment-owned assets without following links out of the model mount."""
    if not configured:
        raise ValueError("Private model directory is not configured")
    directory = Path(configured).resolve(strict=True)
    for name in (
        "gliner_config.json",
        "model.safetensors",
        "config.json",
        "tokenizer_config.json",
        "spm.model",
    ):
        asset = directory / name
        if asset.is_symlink() or not asset.is_file():
            raise ValueError("Invalid private model assets")
    return directory


class RedactionFailure(RuntimeError):
    __slots__ = ()


class RedactionCapacity(RedactionFailure):
    __slots__ = ()


class RedactionRuntime:
    __slots__ = ("active", "private_waiters", "private_gate")

    def __init__(self) -> None:
        self.active = 0
        self.private_waiters = 0
        self.private_gate = asyncio.Lock()

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

    async def run_private(
        self, payload: PrivateBatch, model: str
    ) -> tuple[str, ...]:
        if self.private_waiters >= 4:
            raise RedactionCapacity("Private projection capacity exceeded")
        self.private_waiters += 1
        try:
            async with asyncio.timeout(150), self.private_gate:
                result = await self._worker(
                    json.dumps(payload, ensure_ascii=False).encode(),
                    "galadril_scribe.private_worker",
                    120,
                    cwd=private_model_directory(model),
                )
                return PRIVATE_RESULT.validate_json(result)
        finally:
            self.private_waiters -= 1

    async def run_bytes(self, payload: bytes) -> bytes:
        """Parse private JSON only in the bounded child, keeping the HTTP loop responsive."""
        if self.active >= 2:
            raise RedactionCapacity("Private processing capacity exceeded")
        self.active += 1
        try:
            return await self._worker(
                payload, "galadril_scribe.redaction_worker", 20
            )
        finally:
            self.active -= 1

    async def _worker(
        self,
        payload: bytes,
        module: str,
        timeout: int,
        *,
        cwd: Path | None = None,
    ) -> bytes:
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
                    module,
                    cwd=cwd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env={
                        "PATH": os.defpath,
                        "PYTHONPATH": os.pathsep.join(
                            path for path in sys.path if os.path.isabs(path)
                        ),
                        "LANG": "C.UTF-8",
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "PYTHONSAFEPATH": "1",
                        "HF_HUB_OFFLINE": "1",
                        "TRANSFORMERS_OFFLINE": "1",
                        "TOKENIZERS_PARALLELISM": "false",
                        "OMP_NUM_THREADS": "1",
                        "OPENBLAS_NUM_THREADS": "1",
                        "MKL_NUM_THREADS": "1",
                        "MALLOC_ARENA_MAX": "2",
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

                async with asyncio.timeout(timeout):
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
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    # The child may exit between checking returncode and killing it.
                    pass
                await asyncio.shield(process.wait())
