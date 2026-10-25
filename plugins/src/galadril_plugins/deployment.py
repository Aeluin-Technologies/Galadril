"""Declarative deployments for individually selected packaged microservices."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re

import structlog
from opentelemetry import trace
from pydantic import JsonValue

from galadril_plugins import PluginRegistry


def service_deployment(
    registry: PluginRegistry,
    plugin: str,
    service: str,
    *,
    image: str,
    secret: str,
    namespace: str,
) -> dict[str, JsonValue]:
    """Keeps launch commands and credentials outside tenant-authored pipelines."""
    if (plugin, service) not in registry.services():
        raise ValueError("Unknown packaged plugin service")
    if not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value)
        for value in (secret, namespace)
    ):
        raise ValueError("Invalid deployment namespace or credential reference")
    if not re.fullmatch(r"[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64}", image):
        raise ValueError("Plugin service image must be pinned by digest")
    native = registry.native_command(plugin, service)
    identity = f"{plugin}-{service}"
    suffix = hashlib.sha256(identity.encode()).hexdigest()[:10]
    name = (
        f"plugin-{re.sub(r'[^a-z0-9-]', '-', identity.lower())[:40]}-{suffix}"
    )
    labels: dict[str, JsonValue] = {
        "app": name,
        "galadril.io/component": "plugin",
    }
    with trace.get_tracer("galadril.plugins").start_as_current_span(
        "plugin.deployment.render"
    ):
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": labels,
            },
            "spec": {
                "replicas": 1,
                "strategy": {"type": "Recreate"},
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "automountServiceAccountToken": False,
                        "terminationGracePeriodSeconds": 30,
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 65532,
                            "runAsGroup": 65532,
                            "fsGroup": 65532,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": "service",
                                "image": image,
                                **(
                                    {"command": list(native), "args": []}
                                    if native
                                    else {"args": [plugin, service]}
                                ),
                                "envFrom": [{"secretRef": {"name": secret}}],
                                "securityContext": {
                                    "readOnlyRootFilesystem": True,
                                    "allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "resources": {
                                    "requests": {
                                        "cpu": "100m",
                                        "memory": "128Mi",
                                    },
                                    "limits": {"cpu": "1", "memory": "512Mi"},
                                },
                                "volumeMounts": [
                                    {"name": "tmp", "mountPath": "/tmp"}
                                ],
                            }
                        ],
                        "volumes": [
                            {
                                "name": "tmp",
                                "emptyDir": {
                                    "medium": "Memory",
                                    "sizeLimit": "64Mi",
                                },
                            }
                        ],
                    },
                },
            },
        }


def compose_service(
    registry: PluginRegistry,
    plugin: str,
    service: str,
    *,
    image: str,
    environment_file: str,
) -> dict[str, JsonValue]:
    """Keeps optional listeners outside the default Compose startup profile."""
    if (
        not environment_file
        or len(environment_file) > 4096
        or any(char in environment_file for char in "\r\n\x00")
    ):
        raise ValueError("Invalid deployment environment file")
    deployment = service_deployment(
        registry,
        plugin,
        service,
        image=image,
        secret="unused",
        namespace="galadril",
    )
    metadata = deployment.get("metadata")
    name = metadata.get("name") if isinstance(metadata, dict) else None
    if not isinstance(name, str):
        raise ValueError("Invalid deployment identity")
    return {
        "services": {
            name: {
                "image": image,
                **(
                    {"entrypoint": list(native), "command": []}
                    if (native := registry.native_command(plugin, service))
                    else {"command": [plugin, service]}
                ),
                "profiles": ["plugins"],
                "env_file": [environment_file],
                "read_only": True,
                "user": "65532:65532",
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "tmpfs": ["/tmp:size=64m,mode=1777"],
                "pids_limit": 128,
                "mem_limit": "512m",
                "cpus": 1,
                "init": True,
                "restart": "unless-stopped",
                "stop_grace_period": "30s",
                "logging": {
                    "driver": "json-file",
                    "options": {"max-size": "10m", "max-file": "3"},
                },
            }
        }
    }


def main() -> None:
    """Keep discovery telemetry on stderr so rendered manifests remain parseable."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(
        logger_factory=structlog.stdlib.LoggerFactory(),
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
    )
    parser = argparse.ArgumentParser(
        description="Render one explicit plugin service Kubernetes deployment as JSON."
    )
    parser.add_argument("plugin")
    parser.add_argument("service")
    parser.add_argument("--image", required=True)
    parser.add_argument("--secret")
    parser.add_argument(
        "--format", choices=("kubernetes", "compose"), default="kubernetes"
    )
    parser.add_argument("--environment-file")
    parser.add_argument("--namespace", default="galadril")
    args = parser.parse_args()
    try:
        registry = PluginRegistry.discover()
        deployment = (
            compose_service(
                registry,
                args.plugin,
                args.service,
                image=args.image,
                environment_file=args.environment_file or "",
            )
            if args.format == "compose"
            else service_deployment(
                registry,
                args.plugin,
                args.service,
                image=args.image,
                secret=args.secret or "",
                namespace=args.namespace,
            )
        )
    except ValueError:
        parser.exit(2, "Invalid or unavailable plugin service deployment.\n")
    print(json.dumps(deployment, indent=2))


if __name__ == "__main__":
    main()
