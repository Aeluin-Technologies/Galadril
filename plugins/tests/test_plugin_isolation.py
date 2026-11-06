"""A broken deployment plugin must not take healthy plugins out of service."""

from collections.abc import Callable, MutableMapping
from pathlib import Path
from typing import cast

import pytest
from galadril_pipeline.capabilities import (
    Capability,
    CapabilityKind,
    CapabilityTarget,
)
from galadril_pipeline.config import PipelineStep
from galadril_plugins import PluginRegistry, PluginScope
from galadril_plugins import registry as module
from galadril_plugins.registry import Binding, PluginManifest, UnavailablePlugin


def manifest(identifier: str, *, model: bool = False) -> PluginManifest:
    kind = CapabilityKind.MODEL if model else CapabilityKind.ALGORITHM
    capability = Capability(
        id=f"{identifier}.{kind.value}.first",
        plugin_id=identifier,
        plugin_version="1.0.0",
        kind=kind,
        targets=(CapabilityTarget.SCRIBE,)
        if model
        else (
            CapabilityTarget.PIPELINE,
            CapabilityTarget.SCRIBE,
        ),
        step_type=None if model else "algorithm",
        model_class="deployment.fixture" if model else None,
        name=identifier,
        description="Isolation fixture",
        parameters_schema={"type": "object"},
    )
    factory = f"galadril_plugins.{identifier}.tools.Factory"
    return PluginManifest(
        id=identifier,
        version="1.0.0",
        capabilities=(capability,),
        bindings={
            capability.id: Binding(
                pipeline=None
                if model
                else f"galadril_plugins.{identifier}.pipeline.Factory",
                scribe=None if model else factory,
                model=factory if model else None,
            )
        },
    )


@pytest.mark.parametrize("model", [False, True])
def test_missing_factory_disables_only_its_plugin_and_dependents(
    monkeypatch: pytest.MonkeyPatch,
    model: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []

    def factory() -> object:
        return object()

    def load(path: str) -> Callable[..., object]:
        calls.append(path)
        if path.startswith("galadril_plugins.broken."):
            raise ModuleNotFoundError("private deployment detail")
        return factory

    dependent = manifest("dependent", model=model).model_copy(
        update={"requires": {"broken": "1.0.0"}}
    )
    registry = PluginRegistry(
        (
            dependent,
            manifest("healthy", model=model),
            manifest("broken", model=model),
        )
    )
    monkeypatch.setattr(module, "_load", load)
    collect = registry.models if model else registry.toolsets
    assert [capability.plugin_id for capability, _ in collect()] == ["healthy"]
    assert {
        capability.plugin_id for capability in registry.catalog.capabilities
    } == {"healthy"}
    assert set(registry.unavailable) == {"broken", "dependent"}
    assert registry.unavailable["broken"].error_type == "ModuleNotFoundError"
    assert "private deployment detail" not in repr(registry.unavailable)
    telemetry = capsys.readouterr().out
    assert "plugin.unavailable" in telemetry
    assert "private deployment detail" not in telemetry
    attempted = tuple(calls)
    assert [capability.plugin_id for capability, _ in collect()] == ["healthy"]
    assert tuple(calls) == attempted
    with pytest.raises(TypeError):
        cast(MutableMapping[str, UnavailablePlugin], registry.unavailable)[
            "healthy"
        ] = registry.unavailable["broken"]
    with pytest.raises(ValueError, match="Unknown"):
        registry.handler(
            PluginScope("tenant", "pipeline", "revision"),
            PipelineStep.model_validate(
                {
                    "step": "broken",
                    "type": "algorithm",
                    "input_from": ["input"],
                    "capability_id": "broken.algorithm.first",
                }
            ),
        )


def test_a_partially_loaded_plugin_never_publishes_sibling_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = manifest("broken")
    second = value.capabilities[0].model_copy(
        update={"id": "broken.algorithm.second"}
    )
    value = value.model_copy(
        update={
            "capabilities": (*value.capabilities, second),
            "bindings": {
                **value.bindings,
                second.id: Binding(
                    scribe="galadril_plugins.broken.tools.Missing",
                    pipeline="galadril_plugins.broken.pipeline.Factory",
                ),
            },
        }
    )

    def load(path: str) -> Callable[..., object]:
        if path.endswith(".Missing"):
            raise AttributeError("private detail")
        return lambda: object()

    monkeypatch.setattr(module, "_load", load)
    registry = PluginRegistry((value, manifest("healthy")))
    assert [item.plugin_id for item, _ in registry.toolsets()] == ["healthy"]
    assert set(registry.unavailable) == {"broken"}


def test_discovery_quarantines_bad_manifests_and_dependency_cycles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    healthy = manifest("healthy")
    values = [
        healthy,
        manifest("missing").model_copy(
            update={"requires": {"absent": "1.0.0"}}
        ),
        manifest("cyclea").model_copy(update={"requires": {"cycleb": "1.0.0"}}),
        manifest("cycleb").model_copy(update={"requires": {"cyclea": "1.0.0"}}),
    ]
    for value in values:
        directory = tmp_path / value.id
        directory.mkdir()
        (directory / "plugin.json").write_text(
            value.model_dump_json(), encoding="utf-8"
        )
    invalid = tmp_path / "invalid"
    invalid.mkdir()
    (invalid / "plugin.json").write_text(
        '{"secret": "private detail"}', encoding="utf-8"
    )
    monkeypatch.setattr(module, "files", lambda name: tmp_path)
    monkeypatch.setattr(module, "_load", lambda path: lambda: object())
    registry = PluginRegistry.discover()
    assert [item.plugin_id for item, _ in registry.toolsets()] == ["healthy"]
    assert set(registry.unavailable) == {
        "invalid",
        "missing",
        "cyclea",
        "cycleb",
    }
    assert "private detail" not in repr(registry.unavailable)


def test_missing_skill_does_not_remove_another_plugins_skills(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    values: list[PluginManifest] = []
    for identifier in ("broken", "healthy"):
        value = manifest(identifier)
        skill = value.capabilities[0].model_copy(
            update={
                "kind": CapabilityKind.SKILL,
                "id": f"{identifier}.skill.first",
                "targets": (CapabilityTarget.SCRIBE,),
                "step_type": None,
            }
        )
        value = value.model_copy(
            update={
                "capabilities": (skill,),
                "bindings": {skill.id: Binding(skill="skill.md")},
            }
        )
        values.append(value)
        directory = tmp_path / identifier
        directory.mkdir()
        if identifier == "healthy":
            (directory / "skill.md").write_text(
                "Verified instructions.", encoding="utf-8"
            )
    monkeypatch.setattr(
        module, "files", lambda name: tmp_path / name.rsplit(".", 1)[-1]
    )
    registry = PluginRegistry(values)
    assert [skill.capability.plugin_id for skill in registry.skills()] == [
        "healthy"
    ]
    assert registry.unavailable["broken"].error_type == "FileNotFoundError"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
