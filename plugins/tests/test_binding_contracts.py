"""Public metadata cannot import another runtime's execution dependencies."""

from collections.abc import Mapping

import pytest
from galadril_pipeline.config import PipelineStep
from galadril_pipeline.events import PipelineCommand
from galadril_plugins import PluginRegistry, PluginScope
from galadril_plugins import registry as module
from pydantic import JsonValue


def manifest() -> dict[str, object]:
    return {
        "id": "fixture",
        "version": "1.0.0",
        "capabilities": [
            {
                "id": "fixture.algorithm.count",
                "plugin_id": "fixture",
                "plugin_version": "1.0.0",
                "kind": "algorithm",
                "targets": ["pipeline", "scribe"],
                "step_type": "algorithm",
                "name": "Count",
                "description": "Count matching records.",
                "parameters_schema": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "minimum": 1}},
                    "required": ["limit"],
                    "additionalProperties": False,
                },
            }
        ],
        "bindings": {
            "fixture.algorithm.count": {
                "pipeline": "galadril_plugins.fixture.vision.Factory",
                "scribe": "galadril_plugins.fixture.scribe.Factory",
            }
        },
        "services": {"listener": "galadril_plugins.fixture.worker.Factory"},
    }


def test_catalog_and_services_do_not_import_vision_or_scribe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def load(path: str) -> object:
        calls.append(path)
        return lambda: None

    monkeypatch.setattr(module, "_load", load)
    registry = PluginRegistry.from_manifests((manifest(),))
    assert calls == []
    registry.service("fixture", "listener")
    assert calls == ["galadril_plugins.fixture.worker.Factory"]
    registry.toolsets()
    assert calls == [
        "galadril_plugins.fixture.worker.Factory",
        "galadril_plugins.fixture.scribe.Factory",
    ]


def test_parameters_and_version_are_checked_before_constructing_a_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed: list[str] = []

    class Handler:
        async def execute(
            self, command: PipelineCommand, step: PipelineStep
        ) -> dict[str, JsonValue]:
            return command.payload

    def factory(scope: PluginScope, step: PipelineStep) -> Handler:
        constructed.append(step.step)
        return Handler()

    monkeypatch.setattr(module, "_load", lambda path: factory)
    registry = PluginRegistry.from_manifests((manifest(),))
    for parameters, version in [
        ({}, "1.0.0"),
        ({"limit": 0}, "1.0.0"),
        ({"limit": True}, "1.0.0"),
        ({"limit": 2}, "2.0.0"),
    ]:
        step = PipelineStep.model_validate(
            {
                "step": "count",
                "type": "algorithm",
                "input_from": ["input"],
                "capability_id": "fixture.algorithm.count",
                "capability_version": version,
                "params": parameters,
            }
        )
        with pytest.raises(ValueError):
            registry.handler(PluginScope("tenant_a", "root", "a" * 32), step)
    assert constructed == []
    step = PipelineStep.model_validate(
        {
            "step": "count",
            "type": "algorithm",
            "input_from": ["input"],
            "capability_id": "fixture.algorithm.count",
            "capability_version": "1.0.0",
            "params": {"limit": 2, "trigger": "on_demand"},
        }
    )
    registry.handler(PluginScope("tenant_a", "root", "a" * 32), step)
    assert constructed == ["count"]


def test_validation_checks_a_whole_graph_without_importing_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(path: str) -> object:
        raise AssertionError("Validation must not import executable bindings")

    monkeypatch.setattr(module, "_load", forbidden)
    registry = PluginRegistry.from_manifests((manifest(),))
    definition: Mapping[str, object] = {
        "name": "fixture",
        "sources": [
            {"id": "input", "schema_path": "text.avsc", "match_pattern": ".*"}
        ],
        "pipeline": [
            {
                "step": "count",
                "type": "algorithm",
                "input_from": ["input"],
                "capability_id": "fixture.algorithm.count",
                "capability_version": "1.0.0",
                "params": {"limit": 2},
            }
        ],
    }
    registry.validate_pipeline(definition)


def test_independent_steps_check_their_own_model_compatibility() -> None:
    from galadril_pipeline.capabilities import Compatibility
    from galadril_plugins.registry import PluginManifest

    original = PluginManifest.model_validate(manifest())
    first = original.capabilities[0].model_copy(
        update={"compatibility": Compatibility(allowed_models=("model-a",))}
    )
    second = first.model_copy(
        update={
            "id": "fixture.algorithm.second",
            "compatibility": Compatibility(allowed_models=("model-b",)),
        }
    )
    inventory = original.model_copy(
        update={
            "capabilities": (first, second),
            "bindings": {
                first.id: original.bindings[first.id],
                second.id: original.bindings[first.id],
            },
        }
    )
    registry = PluginRegistry((inventory,))
    registry.validate_pipeline(
        {
            "name": "fixture",
            "sources": [
                {
                    "id": "input",
                    "schema_path": "text.avsc",
                    "match_pattern": ".*",
                }
            ],
            "pipeline": [
                {
                    "step": "first",
                    "type": "algorithm",
                    "input_from": ["input"],
                    "model": "model-a",
                    "capability_id": first.id,
                    "params": {"limit": 1},
                },
                {
                    "step": "second",
                    "type": "algorithm",
                    "input_from": ["first"],
                    "model": "model-b",
                    "capability_id": second.id,
                    "params": {"limit": 1},
                },
            ],
        }
    )


def test_input_schema_sees_the_same_defaults_before_and_after_runtime_loading() -> (
    None
):
    from galadril_pipeline.config import Source

    value = {
        "id": "fixture",
        "version": "1.0.0",
        "services": {"listener": "galadril_plugins.fixture.input.Listener"},
        "capabilities": [
            {
                "id": "fixture.input.text",
                "plugin_id": "fixture",
                "plugin_version": "1.0.0",
                "kind": "input",
                "targets": ["pipeline"],
                "step_type": "source",
                "name": "Text",
                "description": "Text input",
                "parameters_schema": {
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string"},
                        "match_pattern": {"type": "string"},
                        "schema_path": {"type": "string"},
                        "parser": {"type": "string"},
                        "source_kind": {"type": "string"},
                        "content_types": {"type": "array"},
                    },
                    "required": ["topic", "parser", "source_kind"],
                    "additionalProperties": False,
                },
            }
        ],
        "bindings": {"fixture.input.text": {"service": "listener"}},
    }
    registry = PluginRegistry.from_manifests((value,))
    source = Source.model_validate(
        {
            "id": "input",
            "schema_path": "text.avsc",
            "match_pattern": ".*",
            "capability_id": "fixture.input.text",
        }
    )
    registry.validate_input("fixture.input.text", source)
    registry.validate_input(
        "fixture.input.text", Source.model_validate(source.model_dump())
    )


def test_transitive_dependencies_can_use_independent_step_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from galadril_pipeline.capabilities import Compatibility
    from galadril_plugins.registry import PluginManifest

    original = PluginManifest.model_validate(manifest())
    capabilities = tuple(
        original.capabilities[0].model_copy(
            update={
                "id": f"fixture.algorithm.step{index}",
                "compatibility": Compatibility(
                    allowed_models=(f"model-{index}",),
                    requires=(f"fixture.algorithm.step{index + 1}",)
                    if index < 2
                    else (),
                ),
            }
        )
        for index in range(3)
    )
    registry = PluginRegistry(
        (
            original.model_copy(
                update={
                    "capabilities": capabilities,
                    "bindings": {
                        item.id: original.bindings[original.capabilities[0].id]
                        for item in capabilities
                    },
                }
            ),
        )
    )
    definition = {
        "name": "chain",
        "sources": [
            {"id": "input", "schema_path": "text.avsc", "match_pattern": ".*"}
        ],
        "pipeline": [
            {
                "step": f"step{index}",
                "type": "algorithm",
                "input_from": ["input" if index == 0 else f"step{index - 1}"],
                "capability_id": item.id,
                "model": f"model-{index}",
                "params": {"limit": 1},
            }
            for index, item in enumerate(capabilities)
        ],
    }
    registry.validate_pipeline(definition)

    class Handler:
        async def execute(
            self, command: PipelineCommand, step: PipelineStep
        ) -> dict[str, JsonValue]:
            return {}

    monkeypatch.setattr(
        module, "_load", lambda path: lambda scope, step: Handler()
    )
    registry.handler(
        PluginScope("tenant", "pipeline", "revision"),
        PipelineStep.model_validate(definition["pipeline"][0]),
        selected_ids=tuple(item.id for item in capabilities),
    )
    with pytest.raises(ValueError):
        registry.handler(
            PluginScope("tenant", "pipeline", "revision"),
            PipelineStep.model_validate(definition["pipeline"][0]),
            selected_ids=tuple(item.id for item in capabilities[:2]),
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
