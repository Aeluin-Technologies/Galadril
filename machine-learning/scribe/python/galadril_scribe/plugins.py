"""Adapts reviewed model factories and validation to native Scribe contracts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import cast

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from galadril_plugins import PluginRegistry
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError
from pydantic_ai.models import Model

from galadril_scribe.contracts import ModelConfig


@dataclass(frozen=True, slots=True)
class RegisteredModel:
    """Keeps provider settings in deployment code rather than run parameters."""

    model: Model
    config: ModelConfig

    def __post_init__(self) -> None:
        if not isinstance(self.model, Model) or not isinstance(
            self.config, ModelConfig
        ):
            raise ValueError("Plugin model must use native Scribe contracts")


def registered_models(
    registry: PluginRegistry, client: httpx.AsyncClient
) -> dict[str, RegisteredModel]:
    result: dict[str, RegisteredModel] = {}
    for capability, factory in registry.models():
        try:
            binding = factory(client)
        except Exception:
            raise ValueError("Plugin model initialization failed") from None
        if not isinstance(binding, RegisteredModel):
            raise ValueError("Plugin model must use native Scribe contracts")
        result[capability.id] = binding
    return result


class _ValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    definition: dict[str, JsonValue]


def install_validation(app: FastAPI, registry: PluginRegistry) -> None:
    """Validates through the same inventory as execution without importing handlers."""

    @app.post("/pipeline/validate")
    async def validate(request: Request) -> JSONResponse:
        body = bytearray()
        with trace.get_tracer("galadril.scribe").start_as_current_span(
            "pipeline.plugins.validate",
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                async with asyncio.timeout(10):
                    async for chunk in request.stream():
                        if len(body) + len(chunk) > 262144:
                            span.set_status(StatusCode.ERROR)
                            return JSONResponse(
                                {"error": "Pipeline definition exceeds limit"},
                                status_code=413,
                            )
                        body.extend(chunk)
                    parsed = _ValidationRequest.model_validate_json(body)
                    registry.validate_pipeline(
                        cast(dict[str, object], parsed.definition)
                    )
            except (ValueError, ValidationError, TimeoutError, RecursionError):
                span.set_status(StatusCode.ERROR)
                return JSONResponse(
                    {
                        "error": "Pipeline capabilities are unavailable or incompatible"
                    },
                    status_code=422,
                )
            return JSONResponse({"valid": True})
