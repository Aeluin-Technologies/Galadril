"""Contracts for deterministic E2E orchestration helpers."""

from __future__ import annotations

import asyncio
import io
import tarfile
from pathlib import Path

import pytest
from assertions import tempo_service_names
from clients import (
    _vision_consumer_groups_have_members,
    mint_token,
    pipeline_execution_failure,
    statuses_by_step,
)
from environment import (
    ComposeEnvironment,
    configuration_archive,
    image_loader_command,
    pipeline_environment,
)

E2E_COMPOSE = Path(__file__).parent / "environment" / "compose.yaml"
E2E_CONNECTORS = Path(__file__).parent / "fixtures" / "connectors.yaml"
E2E_OTEL_COLLECTOR = Path(__file__).parent / "fixtures" / "otel-collector.yaml"
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


def test_pipeline_failure_preserves_step_and_error() -> None:
    """Makes terminal actor failures actionable instead of timing out opaquely."""
    assert (
        pipeline_execution_failure(
            [("infer", "failed", "model fixture unavailable")]
        )
        == "infer: model fixture unavailable"
    )
    assert pipeline_execution_failure([("infer", "completed", None)]) is None


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


def test_minio_delivers_each_s3_notification_without_batch_delay() -> None:
    """Keeps one-object lifecycle tests independent of Kafka batch flushing."""
    compose = E2E_COMPOSE.read_text(encoding="utf-8")
    assert 'MINIO_NOTIFY_KAFKA_BATCH_SIZE_PRIMARY: "1"' in compose


def test_environment_uses_image_native_postgres_data_directory() -> None:
    """Keeps the non-root database image able to initialize its volume."""
    compose = E2E_COMPOSE.read_text(encoding="utf-8")
    assert "PGDATA: /home/postgres/pgdata/data" in compose
    assert "postgres-data:/home/postgres/pgdata/data" in compose
    assert '"listen_addresses=*"' in compose


def test_environment_uses_tempo_three_configuration() -> None:
    """Rejects configuration blocks removed by the pinned Tempo image."""
    tempo = E2E_TEMPO.read_text(encoding="utf-8")
    assert "\ningester:" not in tempo


def test_vision_has_dedicated_shared_memory_for_embedded_ray() -> None:
    """Prevents Ray workers from exhausting Docker's 64 MiB default shm."""
    compose = E2E_COMPOSE.read_text(encoding="utf-8")
    vision = compose.split("\n  vision:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    assert 'shm_size: "3gb"' in vision


def test_vision_uses_a_stable_grpc_resolver_for_embedded_ray() -> None:
    """Prevents c-ares startup races from killing Ray's first node."""
    compose = E2E_COMPOSE.read_text(encoding="utf-8")
    vision = compose.split("\n  vision:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    assert "GRPC_DNS_RESOLVER: native" in vision
    assert 'RAY_raylet_start_wait_time_s: "60"' in vision


def test_vision_readiness_requires_every_runtime_consumer_group() -> None:
    """Prevents database migrations from masquerading as broker readiness."""
    assert not _vision_consumer_groups_have_members(
        {
            "galadril-e2e-ingress": 1,
            "galadril-e2e-cpu": 1,
            "galadril-e2e-gpu": 1,
            "galadril-e2e-causal": 0,
        }
    )
    assert _vision_consumer_groups_have_members(
        {
            "galadril-e2e-ingress": 1,
            "galadril-e2e-cpu": 1,
            "galadril-e2e-gpu": 1,
            "galadril-e2e-causal": 1,
        }
    )


def test_environment_preserves_structured_application_log_bodies() -> None:
    """Keeps fatal OTLP diagnostics visible in Bazel failure artifacts."""
    collector = E2E_OTEL_COLLECTOR.read_text(encoding="utf-8")
    assert "verbosity: detailed" in collector
    assert "filter/drop_debug:" in collector
    assert "severity_number < SEVERITY_NUMBER_INFO" in collector


def test_gateway_jwt_configuration_is_in_bootstrap_fixture() -> None:
    """Keeps nested JWT fields independent of flat environment mapping."""
    connectors = E2E_CONNECTORS.read_text(encoding="utf-8")
    compose = E2E_COMPOSE.read_text(encoding="utf-8")
    assert 'issuer: "https://e2e.galadril.test"' in connectors
    assert 'audience: "galadril-e2e"' in connectors
    assert "-----BEGIN PUBLIC KEY-----" in connectors
    assert "JWT_ISSUER" not in compose
    assert "PUBLIC_KEY_PEM" not in compose


def test_gateway_security_limits_are_explicit_in_bootstrap_fixture() -> None:
    """Keeps denial thresholds deterministic across local and remote runners."""
    connectors = E2E_CONNECTORS.read_text(encoding="utf-8")
    assert "max_body_bytes: 4096" in connectors
    assert "max_graphql_depth: 5" in connectors


def test_environment_mounts_daemon_portable_configuration_volume() -> None:
    """Keeps Docker orchestration independent of daemon host paths."""
    compose = E2E_COMPOSE.read_text(encoding="utf-8")
    assert "${E2E_FIXTURES_DIR}" not in compose
    assert "${E2E_INFRA_DIR}" not in compose
    assert "${E2E_SCHEMAS_DIR}" not in compose
    assert "e2e-config:/e2e:ro" in compose
    assert "external: true" in compose


def test_postgres_healthcheck_waits_for_final_postmaster() -> None:
    """Prevents dependents from observing the temporary initialization server."""
    compose = E2E_COMPOSE.read_text(encoding="utf-8")
    assert 'head -n 1 "$$PGDATA/postmaster.pid"' in compose
    assert '" = 1 && pg_isready' in compose


def test_configuration_archive_contains_required_runfiles() -> None:
    """Ensures the daemon receives every file previously bind-mounted."""
    archive = configuration_archive()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        names = set(bundle.getnames())

    assert {
        "connectors.yaml",
        "otel-collector.yaml",
        "tempo.yaml",
        "site-packages/e2e_inference_model.py",
        "spicedb/schema.zed",
        "003-install-extensions.sh",
        "004-create-galadril-app.sh",
        "010-init-spicedb.sh",
        "020-init-galadril-roles.sh",
    } <= names


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

    async def prepare_configuration(_environment: ComposeEnvironment) -> None:
        calls.append("prepare")

    async def close(_environment: ComposeEnvironment) -> None:
        calls.append("close")

    monkeypatch.setattr(ComposeEnvironment, "__init__", lambda _self: None)
    monkeypatch.setattr(ComposeEnvironment, "load_images", load_images)
    monkeypatch.setattr(
        ComposeEnvironment,
        "prepare_configuration",
        prepare_configuration,
    )
    monkeypatch.setattr(ComposeEnvironment, "start_core", start_core)
    monkeypatch.setattr(ComposeEnvironment, "close", close)

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="startup failed"):
            async with pipeline_environment():
                raise AssertionError(
                    "environment yielded after startup failure"
                )

    asyncio.run(exercise())
    assert calls == ["load", "prepare", "start", "close"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
