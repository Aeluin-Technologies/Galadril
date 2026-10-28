"""Scribe owns generative models while Eru retains schema validation."""

from __future__ import annotations

import httpx
import structlog
from opentelemetry import propagate
from pydantic import BaseModel

from eru.common.exceptions import ReasoningError


class ScribeJsonModel:
    __slots__ = ("_endpoint", "_client")

    def __init__(
        self, endpoint: str, *, client: httpx.Client | None = None
    ) -> None:
        self._endpoint = endpoint.rstrip("/") + "/pipeline/generate"
        self._client = client or httpx.Client(
            follow_redirects=False, trust_env=False
        )

    def close(self) -> None:
        self._client.close()

    def __call__[T: BaseModel](
        self, prompt: str, schema: type[T], *, max_new_tokens: int = 1024
    ) -> T:
        """Bound transport memory and never fall back to an embedded model."""
        headers: dict[str, str] = {}
        propagate.inject(headers)
        try:
            with self._client.stream(
                "POST",
                self._endpoint,
                json={
                    "prompt": prompt,
                    "schema": schema.model_json_schema(),
                    "max_tokens": max_new_tokens,
                },
                headers=headers,
                timeout=120,
            ) as response:
                response.raise_for_status()
                body = bytearray()
                for part in response.iter_bytes():
                    if len(body) + len(part) > 1048576:
                        raise ValueError("Scribe output exceeds limit")
                    body.extend(part)
            return schema.model_validate_json(body)
        except Exception as error:
            structlog.get_logger(__name__).warning(
                "eru.scribe_generation_failed", error_type=type(error).__name__
            )
            raise ReasoningError(
                "Scribe structured generation failed"
            ) from None
