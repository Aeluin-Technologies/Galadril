"""Service deployment is explicit, declarative, and independent from discovery."""

import json
import sys

import pytest
from galadril_plugins import PluginRegistry
from galadril_plugins.deployment import compose_service, service_deployment


def test_oversized_digest_pinned_image_is_rejected() -> None:
    registry = PluginRegistry.from_manifests(
        (
            {
                "id": "fixture",
                "version": "1.0.0",
                "capabilities": [],
                "bindings": {},
                "services": {"listener": "fixture.Listener"},
            },
        )
    )
    with pytest.raises(ValueError, match="image exceeds limit"):
        service_deployment(
            registry,
            "fixture",
            "listener",
            image="registry/" + "a" * 65536 + "@sha256:" + "a" * 64,
            secret="plugin-fixture",
            namespace="galadril",
        )


def test_selected_service_produces_a_scoped_hardened_deployment() -> None:
    registry = PluginRegistry.from_manifests(
        (
            {
                "id": "fixture",
                "version": "1.0.0",
                "capabilities": [],
                "bindings": {},
                "services": {
                    "listener": "galadril_plugins.fixture.listener.Service"
                },
            },
        )
    )
    deployment = service_deployment(
        registry,
        "fixture",
        "listener",
        image="registry/plugins@sha256:" + "a" * 64,
        secret="plugin-fixture",
        namespace="galadril",
    )
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["args"] == ["fixture", "listener"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert pod["automountServiceAccountToken"] is False
    assert container["envFrom"] == [{"secretRef": {"name": "plugin-fixture"}}]
    assert pod["terminationGracePeriodSeconds"] == 30
    assert "galadril_plugins.fixture.listener" not in str(deployment)
    compose = compose_service(
        registry,
        "fixture",
        "listener",
        image="registry/plugins@sha256:" + "a" * 64,
        environment_file="./.local/plugin.env",
    )
    worker = next(iter(compose["services"].values()))
    assert worker["profiles"] == ["plugins"]
    assert worker["command"] == ["fixture", "listener"]
    assert worker["read_only"] is True
    assert worker["env_file"] == ["./.local/plugin.env"]
    assert worker["stop_grace_period"] == "30s"
    with pytest.raises(ValueError):
        service_deployment(
            registry,
            "fixture",
            "absent",
            image="image:tag",
            secret="secret",
            namespace="galadril",
        )
    with pytest.raises(ValueError):
        service_deployment(
            registry,
            "fixture",
            "listener",
            image="image:latest",
            secret="../secret",
            namespace="galadril",
        )


def test_deployment_cli_writes_only_machine_readable_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from galadril_plugins import deployment

    def discover() -> PluginRegistry:
        return PluginRegistry.from_manifests(
            (
                {
                    "id": "fixture",
                    "version": "1.0.0",
                    "capabilities": [],
                    "bindings": {},
                    "services": {
                        "listener": "galadril_plugins.fixture.Listener"
                    },
                },
            )
        )

    monkeypatch.setattr(PluginRegistry, "discover", discover)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "deployment",
            "fixture",
            "listener",
            "--image",
            "registry/plugins@sha256:" + "a" * 64,
            "--secret",
            "plugin-fixture",
        ],
    )
    deployment.main()
    assert json.loads(capsys.readouterr().out)["kind"] == "Deployment"


def test_native_services_launch_only_the_packaged_binary() -> None:
    registry = PluginRegistry.from_manifests(
        (
            {
                "id": "fixture",
                "version": "1.0.0",
                "capabilities": [],
                "bindings": {},
                "services": {
                    "tracker": {"runtime": "native", "command": ["/tracker"]}
                },
            },
        )
    )
    deployment = service_deployment(
        registry,
        "fixture",
        "tracker",
        image="registry/tracker@sha256:" + "a" * 64,
        secret="capture-key",
        namespace="galadril",
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == ["/tracker"]
    assert container["args"] == []
    compose = compose_service(
        registry,
        "fixture",
        "tracker",
        image="registry/tracker@sha256:" + "a" * 64,
        environment_file="./tracker.env",
    )
    worker = next(iter(compose["services"].values()))
    assert worker["entrypoint"] == ["/tracker"]
    assert worker["command"] == []
    with pytest.raises(ValueError, match="native"):
        registry.service("fixture", "tracker")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
