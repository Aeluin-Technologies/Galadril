"""Deployment discovery must bind metadata to executable, tenant-owned code."""

import asyncio
from types import SimpleNamespace

import pytest
from galadril_pipeline.config import PipelineStep
from galadril_pipeline.events import PipelineCommand
from galadril_plugins import PluginRegistry, PluginScope


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "namespace",
        "unbound",
        "dependency",
        "import",
        "route",
        "pipeline",
        "forward",
    ],
)
def test_invalid_bundle_fails_before_importing_code(mutation: str) -> None:
    manifest = {
        "id": "example",
        "version": "1.0.0",
        "capabilities": [
            {
                "id": "example.algorithm.test",
                "plugin_id": "example",
                "plugin_version": "1.0.0",
                "kind": "algorithm",
                "targets": ["pipeline"],
                "step_type": "algorithm",
                "name": "Example",
                "description": "Test registration",
                "parameters_schema": {"type": "object"},
            }
        ],
        "bindings": {
            "example.algorithm.test": {
                "pipeline": "galadril_plugins.example.handlers.Factory"
            }
        },
    }
    if mutation == "duplicate":
        manifest["capabilities"].append(manifest["capabilities"][0])
    elif mutation == "namespace":
        manifest["id"] = "other"
    elif mutation == "unbound":
        manifest["bindings"] = {}
    elif mutation == "dependency":
        manifest["requires"] = {"absent": "1.0.0"}
    elif mutation in {"route", "pipeline", "forward"}:
        manifest["capabilities"][0]["step_type"] = mutation
    else:
        manifest["bindings"]["example.algorithm.test"]["pipeline"] = "os.system"
    with pytest.raises(ValueError):
        PluginRegistry.from_manifests((manifest,))


def test_service_worker_runs_only_an_explicit_registered_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from galadril_plugins.worker import serve

    calls: list[str] = []

    class Service:
        async def run(self, stop: asyncio.Event) -> None:
            calls.append("run")
            stop.set()

    def factory(environment: dict[str, str]) -> Service:
        assert environment == {"deployment": "trusted"}
        return Service()

    registry = SimpleNamespace(service=lambda plugin, name: factory)
    monkeypatch.setattr(PluginRegistry, "discover", lambda: registry)
    asyncio.run(serve("example", "input", {"deployment": "trusted"}))
    assert calls == ["run"]


def test_model_and_algorithm_bindings_use_registered_factories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from galadril_plugins import registry as module

    manifest = {
        "id": "example",
        "version": "1.0.0",
        "capabilities": [],
        "bindings": {},
    }
    capabilities: list[dict[str, object]] = []
    bindings: dict[str, object] = {}
    seen: list[str] = []

    class Handler:
        async def execute(
            self, command: PipelineCommand, step: PipelineStep
        ) -> dict[str, str]:
            return {"selected": step.capability_id or ""}

    def factory(scope: PluginScope, step: PipelineStep) -> Handler:
        seen.append(scope.tenant_id)
        return Handler()

    monkeypatch.setattr(module, "_load", lambda path: factory)
    for kind, step_type in (("model", "inference"), ("algorithm", "algorithm")):
        identifier = f"example.{kind}.custom"
        capabilities.append(
            {
                "id": identifier,
                "plugin_id": "example",
                "plugin_version": "1.0.0",
                "kind": kind,
                "targets": ["pipeline"],
                "name": kind,
                "description": "Custom execution",
                "step_type": step_type,
                "parameters_schema": {"type": "object"},
                **(
                    {"model_class": "deployment.custom"}
                    if kind == "model"
                    else {}
                ),
            }
        )
        bindings[identifier] = {
            "pipeline": "galadril_plugins.example.handlers.Factory"
        }
    manifest.update(capabilities=capabilities, bindings=bindings)
    registry = PluginRegistry.from_manifests((manifest,))
    for item in capabilities:
        step = PipelineStep.model_validate(
            {
                "step": "custom",
                "type": item["step_type"],
                "input_from": ["input"],
                "capability_id": item["id"],
            }
        )
        registry.handler(PluginScope("a", "p", "r"), step)
    assert seen == ["a", "a"]


def test_input_discovery_does_not_start_its_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from galadril_plugins import registry as module

    starts: list[str] = []

    def factory(environment: dict[str, str]) -> object:
        starts.append(environment["tenant"])
        return object()

    monkeypatch.setattr(module, "_load", lambda path: factory)
    manifest = {
        "id": "example",
        "version": "1.0.0",
        "capabilities": [
            {
                "id": "example.input.custom",
                "plugin_id": "example",
                "plugin_version": "1.0.0",
                "kind": "input",
                "targets": ["pipeline"],
                "step_type": "source",
                "name": "Custom input",
                "description": "A separately deployed listener",
                "parameters_schema": {"type": "object"},
            }
        ],
        "bindings": {"example.input.custom": {"service": "listener"}},
        "services": {"listener": "galadril_plugins.example.listener.Service"},
    }
    registry = PluginRegistry.from_manifests((manifest,))
    registry.validate_input("example.input.custom")
    assert starts == []
    registry.service("example", "listener")({"tenant": "tenant_a"})
    assert starts == ["tenant_a"]


def test_native_routing_is_not_advertised_as_a_plugin() -> None:
    registry = PluginRegistry.discover()
    assert not any(
        item.id.startswith("routing.") for item in registry.catalog.capabilities
    )
    with pytest.raises(ValueError, match="Unknown plugin service"):
        registry.service("routing", "imap")


def test_service_shutdown_has_a_deadline_and_releases_resources() -> None:
    from galadril_plugins.worker import run_service

    cleaned: list[bool] = []

    class Service:
        async def run(self, stop: asyncio.Event) -> None:
            try:
                stop.set()
                await asyncio.Event().wait()
            finally:
                cleaned.append(True)

    async def run() -> None:
        with pytest.raises(RuntimeError, match="shutdown"):
            await run_service(Service(), asyncio.Event(), grace_seconds=0.01)

    asyncio.run(run())
    assert cleaned == [True]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
