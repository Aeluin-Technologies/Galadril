"""Regression checks for published revision loading and deployment isolation."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from galadril_ontology.backends.terminus import TerminusDatabase
from galadril_vision.common.config import VisionConfig
from galadril_vision.common.pipelines import (
    PipelineRuntimeRegistry,
    PipelineUnavailable,
    load_published_pipeline,
    load_published_pipelines,
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
        }
    )


def test_published_loader_scopes_transaction_and_pins_runtime_identity() -> (
    None
):
    client = MagicMock()
    client.read = AsyncMock(
        side_effect=[
            (
                "newhead",
                [
                    {
                        "@id": "pipeline/daily",
                        "pipeline_id": "daily",
                        "published_revision_id": "a" * 32,
                        "deleted_at_ms": None,
                    }
                ],
            ),
            (
                "a" * 32,
                [
                    {
                        "@id": "pipeline/daily",
                        "definition": {
                            "name": "daily",
                            "sources": [],
                            "pipeline": [],
                        },
                    }
                ],
            ),
        ]
    )
    client.close = AsyncMock()
    with patch(
        "galadril_vision.common.pipelines.TerminusClient", return_value=client
    ):
        config = asyncio.run(
            load_published_pipeline(bootstrap(), "tenant_a", "daily")
        )
    assert config.runtime_tenant_id == "tenant_a"
    assert config.name == f"tenant_a/daily/{'a' * 32}"
    assert config.ontology_pipeline_id == "daily"
    assert client.read.call_args_list[0].args == ("tenant_a",)
    assert client.read.call_args_list[1].kwargs == {
        "ref": "a" * 32,
        "commit": True,
    }
    client.close.assert_awaited_once()


def test_missing_publication_fails_closed() -> None:
    client = MagicMock()
    client.read = AsyncMock(return_value=("head", []))
    client.close = AsyncMock()
    with patch(
        "galadril_vision.common.pipelines.TerminusClient", return_value=client
    ):
        with pytest.raises(PipelineUnavailable):
            asyncio.run(
                load_published_pipeline(bootstrap(), "tenant_b", "daily")
            )


@pytest.mark.parametrize(
    "tenant", ["", "../tenant_b", "tenant_a/tenant_b", " tenant_a"]
)
def test_invalid_tenant_never_opens_database(tenant: str) -> None:
    with patch("galadril_vision.common.pipelines.TerminusClient") as connect:
        with pytest.raises(PipelineUnavailable):
            asyncio.run(load_published_pipeline(bootstrap(), tenant, "daily"))
    connect.assert_not_called()


def test_catalog_loads_every_published_pipeline_for_every_configured_tenant() -> (
    None
):
    client = MagicMock()
    client.read = AsyncMock(
        side_effect=[
            (
                "tenant-a-head",
                [
                    {
                        "@id": "pipeline/daily",
                        "pipeline_id": "daily",
                        "published_revision_id": "a" * 32,
                        "deleted_at_ms": None,
                    },
                    {
                        "@id": "pipeline/hourly",
                        "pipeline_id": "hourly",
                        "published_revision_id": "b" * 32,
                        "deleted_at_ms": None,
                    },
                ],
            ),
            (
                "a" * 32,
                [
                    {
                        "@id": "pipeline/daily",
                        "definition": _definition("camera"),
                    }
                ],
            ),
            (
                "b" * 32,
                [
                    {
                        "@id": "pipeline/hourly",
                        "definition": _definition("sensor"),
                    }
                ],
            ),
            (
                "tenant-b-head",
                [
                    {
                        "@id": "pipeline/daily",
                        "pipeline_id": "daily",
                        "published_revision_id": "c" * 32,
                        "deleted_at_ms": None,
                    }
                ],
            ),
            (
                "c" * 32,
                [
                    {
                        "@id": "pipeline/daily",
                        "definition": _definition("camera"),
                    }
                ],
            ),
        ]
    )
    client.close = AsyncMock()
    with patch(
        "galadril_vision.common.pipelines.TerminusClient", return_value=client
    ):
        config = bootstrap()
        config.connectors.terminusdb.tenants.update(
            {
                "tenant_a": TerminusDatabase(
                    database="tenant_a", user="reader_a", password="secret"
                ),
                "tenant_b": TerminusDatabase(
                    database="tenant_b", user="reader_b", password="secret"
                ),
            }
        )
        configs = asyncio.run(load_published_pipelines(config))

    assert [config.name for config in configs] == [
        f"tenant_a/daily/{'a' * 32}",
        f"tenant_a/hourly/{'b' * 32}",
        f"tenant_b/daily/{'c' * 32}",
    ]
    assert all(item.connectors is config.connectors for item in configs)
    assert all(item.ray is config.ray for item in configs)
    assert client.read.call_args_list[0].args == ("tenant_a",)
    assert client.read.call_args_list[3].args == ("tenant_b",)
    client.close.assert_awaited_once()


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
