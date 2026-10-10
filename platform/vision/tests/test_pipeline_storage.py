"""Regression checks for published revision loading and deployment isolation."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import grpc
import orjson
import pytest
from galadril_registry_api import PipelineArtifact
from galadril_vision.common.config import VisionConfig
from galadril_vision.common.pipelines import (
    PipelinePending,
    PipelineRuntimeRegistry,
    PipelineUnavailable,
    load_published_pipeline,
    wait_for_published_pipeline,
)
from galadril_vision.common.schemas import CanonicalRecord
from galadril_vision.streaming.app import _MultiPipelineIngress
from galadril_vision.streaming.handlers import AvroEnvelope, IngressHandler
from galadril_vision.streaming.topics import TopicLayout


def bootstrap() -> VisionConfig:
    return VisionConfig.model_validate(
        {
            "name": "service",
            "connectors": {
                "kafka": {
                    "brokers": ["kafka:9092"],
                    "schema_registry": "http://kafka:8081",
                    "consumer_group": "test",
                },
                "s3": {
                    "endpoint": "http://s3:9000",
                    "access_key": "a",
                    "secret_key": "b",
                    "region": "us-east-1",
                    "bucket": "raw",
                },
                "postgres": {
                    "database": "test",
                    "host": "postgres",
                    "user": "app",
                    "password": "secret",
                },
                "spicedb": {"endpoint": "spicedb:50051", "token": "secret"},
            },
            "registry": {"endpoint": "http://registry:50052"},
        }
    )


def test_published_loader_scopes_transaction_and_pins_runtime_identity() -> (
    None
):
    client = MagicMock()
    client.get_runtime_pipeline = AsyncMock(
        return_value=PipelineArtifact(
            pipeline_id="daily",
            revision_id="a" * 32,
            definition_json=b'{"name":"daily","sources":[],"pipeline":[]}',
        )
    )
    client.close = AsyncMock()
    client.validate_tenants = AsyncMock(
        return_value=(MagicMock(tenant_id="tenant_a", exists=True),)
    )
    with patch(
        "galadril_vision.common.pipelines.RegistryClient", return_value=client
    ):
        config = asyncio.run(
            load_published_pipeline(bootstrap(), "tenant_a", "daily", "a" * 32)
        )
    assert config.runtime_tenant_id == "tenant_a"
    assert config.name == f"tenant_a/daily/{'a' * 32}"
    assert config.ontology_pipeline_id == "daily"
    client.get_runtime_pipeline.assert_awaited_once_with(
        "tenant_a", "daily", "a" * 32
    )
    client.close.assert_awaited_once()


def test_missing_publication_fails_closed() -> None:
    client = MagicMock()
    client.list_published_pipelines = AsyncMock(return_value=())
    client.close = AsyncMock()
    client.validate_tenants = AsyncMock(
        return_value=(MagicMock(tenant_id="tenant_b", exists=True),)
    )
    with patch(
        "galadril_vision.common.pipelines.RegistryClient", return_value=client
    ):
        with pytest.raises(PipelinePending):
            asyncio.run(
                load_published_pipeline(bootstrap(), "tenant_b", "daily")
            )


def test_missing_tenant_waits_without_loading_a_pipeline() -> None:
    client = MagicMock()
    client.close = AsyncMock()
    client.validate_tenants = AsyncMock(
        return_value=(MagicMock(tenant_id="tenant_a", exists=False),)
    )
    with patch(
        "galadril_vision.common.pipelines.RegistryClient", return_value=client
    ):
        with pytest.raises(PipelinePending):
            asyncio.run(
                load_published_pipeline(bootstrap(), "tenant_a", "daily")
            )
    client.list_published_pipelines.assert_not_called()
    client.close.assert_awaited_once()


class _RegistryError(grpc.RpcError):
    def __init__(self, status: grpc.StatusCode) -> None:
        self._status = status

    def code(self) -> grpc.StatusCode:
        return self._status


@pytest.mark.parametrize("status", list(grpc.StatusCode))
def test_registry_retries_only_transient_or_missing_resources(
    status: grpc.StatusCode,
) -> None:
    client = MagicMock()
    client.close = AsyncMock()
    client.validate_tenants = AsyncMock(side_effect=_RegistryError(status))
    pending = status in (
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.DEADLINE_EXCEEDED,
        grpc.StatusCode.NOT_FOUND,
    )
    with patch(
        "galadril_vision.common.pipelines.RegistryClient", return_value=client
    ):
        with pytest.raises(PipelineUnavailable) as failure:
            asyncio.run(
                load_published_pipeline(bootstrap(), "tenant_a", "daily")
            )
    assert isinstance(failure.value, PipelinePending) is pending
    client.close.assert_awaited_once()


@pytest.mark.parametrize(
    "validation",
    [(), (MagicMock(tenant_id="another_tenant", exists=True),)],
)
def test_invalid_tenant_validation_is_fatal(
    validation: tuple[object, ...],
) -> None:
    client = MagicMock()
    client.close = AsyncMock()
    client.validate_tenants = AsyncMock(return_value=validation)
    with patch(
        "galadril_vision.common.pipelines.RegistryClient", return_value=client
    ):
        with pytest.raises(PipelineUnavailable) as failure:
            asyncio.run(
                load_published_pipeline(bootstrap(), "tenant_a", "daily")
            )
    assert not isinstance(failure.value, PipelinePending)
    client.list_published_pipelines.assert_not_called()
    client.close.assert_awaited_once()


def test_startup_wait_preserves_scope_and_caps_backoff() -> None:
    config = bootstrap()
    load = AsyncMock(
        side_effect=[PipelinePending("pending") for _ in range(8)] + [config]
    )
    sleep = AsyncMock()
    with (
        patch("galadril_vision.common.pipelines.load_published_pipeline", load),
        patch("galadril_vision.common.pipelines.asyncio.sleep", sleep),
    ):
        resolved = asyncio.run(
            wait_for_published_pipeline(config, "tenant_a", "daily", "a" * 32)
        )
    assert resolved is config
    assert load.await_count == 9
    for call in load.await_args_list:
        assert call.args == (config, "tenant_a", "daily", "a" * 32)
    assert [call.args[0] for call in sleep.await_args_list] == [
        1.0,
        2.0,
        4.0,
        8.0,
        16.0,
        30.0,
        30.0,
        30.0,
    ]


@pytest.mark.parametrize(
    "error", [PipelineUnavailable("invalid"), asyncio.CancelledError()]
)
def test_startup_does_not_retry_fatal_errors_or_cancellation(
    error: BaseException,
) -> None:
    with (
        patch(
            "galadril_vision.common.pipelines.load_published_pipeline",
            AsyncMock(side_effect=error),
        ) as load,
        patch(
            "galadril_vision.common.pipelines.asyncio.sleep", AsyncMock()
        ) as sleep,
    ):
        with pytest.raises(type(error)):
            asyncio.run(
                wait_for_published_pipeline(bootstrap(), "tenant_a", "daily")
            )
    load.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.parametrize(
    "tenant", ["", "../tenant_b", "tenant_a/tenant_b", " tenant_a"]
)
def test_invalid_tenant_never_opens_database(tenant: str) -> None:
    with patch("galadril_vision.common.pipelines.RegistryClient") as connect:
        with pytest.raises(PipelineUnavailable):
            asyncio.run(load_published_pipeline(bootstrap(), tenant, "daily"))
    connect.assert_not_called()


def test_runtime_registry_routes_each_tenant_without_cross_tenant_fallback() -> (
    None
):
    first = _published_config("tenant_a", "daily", "a" * 32, "camera")
    companion = _published_config("tenant_a", "archive", "c" * 32, "camera")
    second = _published_config("tenant_b", "daily", "b" * 32, "camera")
    registry = PipelineRuntimeRegistry((first, companion, second))

    assert registry.for_ingress("tenant_a", "camera") == (first, companion)
    assert registry.for_ingress("tenant_b", "camera") == (second,)
    assert registry.for_ingress("tenant_c", "camera") == ()
    assert registry.for_command("tenant_a", first.name) is first
    with pytest.raises(PipelineUnavailable):
        registry.for_command("tenant_b", first.name)


def test_shared_ingress_dispatches_only_matching_tenant_handlers() -> None:
    first = _published_config("tenant_a", "daily", "a" * 32, "camera")
    companion = _published_config("tenant_a", "archive", "c" * 32, "camera")
    second = _published_config("tenant_b", "daily", "b" * 32, "camera")
    first_handler = MagicMock()
    first_handler.handle_record = AsyncMock(return_value=())
    first_handler.reject = AsyncMock(return_value=())
    second_handler = MagicMock()
    second_handler.handle_record = AsyncMock(return_value=())
    second_handler.reject = AsyncMock(return_value=())
    companion_handler = MagicMock()
    companion_handler.handle_record = AsyncMock(return_value=())
    companion_handler.reject = AsyncMock(return_value=())
    ingress = _MultiPipelineIngress(
        PipelineRuntimeRegistry((first, companion, second)),
        {
            first.name: first_handler,
            companion.name: companion_handler,
            second.name: second_handler,
        },
    )
    record = CanonicalRecord(
        record_id="record",
        tenant_id="tenant_a",
        source="camera",
        input_type="image",
    )
    envelope = AvroEnvelope(
        source_id="schema-type",
        topic="raw",
        tenant_id="tenant_a",
        pipeline_id="daily",
        revision_id="a" * 32,
        payload={},
    )

    with patch(
        "galadril_vision.streaming.app.EventNormalizer.normalize",
        return_value=record.model_dump(mode="json"),
    ):
        asyncio.run(ingress.handle(envelope))

    first_handler.handle_record.assert_awaited_once()
    assert (
        first_handler.handle_record.await_args.kwargs["source_id"] == "camera"
    )
    second_handler.handle_record.assert_not_awaited()
    companion_handler.handle_record.assert_not_awaited()


def _definition(source_id: str) -> dict[str, object]:
    return {
        "name": "stored-name-is-not-authoritative",
        "sources": [
            {
                "id": source_id,
                "topic": "raw",
                "match_pattern": ".*",
                "schema_path": "schemas/raw.avsc",
            }
        ],
        "pipeline": [],
    }


def _json_definition(source_id: str) -> bytes:
    """Serializes a Registry artifact using the production JSON codec."""
    return orjson.dumps(_definition(source_id))


def _published_config(
    tenant: str, pipeline: str, revision: str, source_id: str
) -> VisionConfig:
    config = VisionConfig.with_pipeline(
        bootstrap().model_dump(), _definition(source_id)
    )
    config.name = f"{tenant}/{pipeline}/{revision}"
    config.runtime_tenant_id = tenant
    config.runtime_pipeline_id = pipeline
    config.runtime_revision_id = revision
    return config


def test_runtime_rejects_other_tenants_and_revisions() -> None:
    config = bootstrap()
    config.name = "tenant_a/daily/revision_a"
    config.runtime_tenant_id = "tenant_a"
    assert config.accepts_command("tenant_a", config.name)
    assert not config.accepts_command("tenant_b", config.name)
    assert not config.accepts_command(None, config.name)
    assert not config.accepts_command("tenant_a", "tenant_a/daily/revision_b")


def test_local_pipeline_keeps_its_stable_ontology_binding_id() -> None:
    config = bootstrap()
    assert config.ontology_pipeline_id == config.name


def test_ingress_never_routes_another_tenants_record() -> None:
    publisher = MagicMock()
    publisher.publish = AsyncMock()
    record = CanonicalRecord(
        record_id="record",
        tenant_id="tenant_b",
        source="camera",
        input_type="image",
    )
    handler = IngressHandler(
        pipeline="tenant_a/daily/rev",
        tenant_id="tenant_a",
        routes=MagicMock(),
        publisher=publisher,
        topics=TopicLayout(),
        metrics=MagicMock(),
    )
    with patch(
        "galadril_vision.streaming.handlers.EventNormalizer.normalize",
        return_value=record.model_dump(),
    ):
        commands = asyncio.run(
            handler.handle(
                AvroEnvelope(
                    source_id="source",
                    topic="raw",
                    tenant_id="tenant_b",
                    pipeline_id="daily",
                    revision_id="revision_b",
                    payload={},
                )
            )
        )
    assert commands == ()
    publisher.publish.assert_not_awaited()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
