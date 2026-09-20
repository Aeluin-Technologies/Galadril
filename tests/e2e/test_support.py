"""Contracts for deterministic E2E orchestration helpers."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from assertions import tempo_service_names
from clients import mint_token, statuses_by_step
from environment import (
    ComposeEnvironment,
    image_loader_command,
    pipeline_environment,
)

E2E_COMPOSE = Path(__file__).parent / "environment" / "compose.yaml"
E2E_TEMPO = Path(__file__).parent / "fixtures" / "tempo.yaml"


def test_minted_es256_token_has_compact_jwt_shape() -> None:
    """Keeps the test issuer compatible with Gateway's strict extractor."""
    assert len(mint_token("principal").split(".")) == 3


def test_lineage_statuses_are_grouped_without_losing_transitions() -> None:
    """Makes lifecycle failures readable without weakening exact assertions."""
    events = [
        {"step": "infer", "status": "accepted"},
        {"step": "infer", "status": "completed"},
        {"step": "sink", "status": "running"},
    ]

    assert statuses_by_step(events) == {
        "infer": {"accepted", "completed"},
        "sink": {"running"},
    }


def test_tempo_service_names_read_otlp_resource_attributes() -> None:
    """Ensures trace assertions use Tempo's OTLP response structure."""
    trace = {
        "batches": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.name",
                            "value": {"stringValue": "galadril-vision"},
                        }
                    ]
                }
            }
        ]
    }

    assert tempo_service_names(trace) == {"galadril-vision"}


def test_image_loader_command_does_not_require_executable_runfile(
    tmp_path: Path,
) -> None:
    """Keeps remotely materialized OCI launchers usable without mode bits."""
    loader = tmp_path / "load_gateway.sh"
    loader.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    loader.chmod(0o644)

    assert image_loader_command(loader) == ("/bin/bash", str(loader))


def test_environment_uses_pinned_official_minio_images() -> None:
    """Prevents remote tests from relying on removed Docker Hub images."""
    compose = E2E_COMPOSE.read_text(encoding="utf-8")
    assert "image: quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z" in compose
    assert "image: quay.io/minio/mc:RELEASE.2025-08-13T08-35-41Z" in compose
    assert "image: minio/minio:latest" not in compose
    assert "image: minio/mc:latest" not in compose


def test_environment_uses_image_native_postgres_data_directory() -> None:
    """Keeps the non-root database image able to initialize its volume."""
    compose = E2E_COMPOSE.read_text(encoding="utf-8")
    assert "PGDATA: /home/postgres/pgdata/data" in compose
    assert "postgres-data:/home/postgres/pgdata/data" in compose


def test_environment_uses_tempo_three_configuration() -> None:
    """Rejects configuration blocks removed by the pinned Tempo image."""
    tempo = E2E_TEMPO.read_text(encoding="utf-8")
    assert "\ningester:" not in tempo


def test_environment_tears_down_after_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prevents leaked containers when Compose fails before yielding."""
    calls: list[str] = []

    async def load_images(_environment: ComposeEnvironment) -> None:
        calls.append("load")

    async def start_core(_environment: ComposeEnvironment) -> None:
        calls.append("start")
        raise RuntimeError("startup failed")

    async def close(_environment: ComposeEnvironment) -> None:
        calls.append("close")

    monkeypatch.setattr(ComposeEnvironment, "__init__", lambda _self: None)
    monkeypatch.setattr(ComposeEnvironment, "load_images", load_images)
    monkeypatch.setattr(ComposeEnvironment, "start_core", start_core)
    monkeypatch.setattr(ComposeEnvironment, "close", close)

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="startup failed"):
            async with pipeline_environment():
                raise AssertionError(
                    "environment yielded after startup failure"
                )

    asyncio.run(exercise())
    assert calls == ["load", "start", "close"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
