"""Bounds cluster lifetime so a failed Bazel test cannot reuse user state."""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def run(args: list[str], *, check: bool = True, timeout: int = 300) -> str:
    """Preserves bounded command diagnostics in Bazel test logs."""
    print(json.dumps({"command": args, "timeout_seconds": timeout}), flush=True)
    result = subprocess.run(
        args,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode:
        print(result.stdout[-20000:], file=sys.stderr, flush=True)
        raise subprocess.CalledProcessError(result.returncode, args)
    return result.stdout


@contextmanager
def isolated_cluster(tools: Path, state: Path) -> Iterator[None]:
    """Deletes only the uniquely named cluster created for this invocation."""
    name = "galadril-test-" + uuid.uuid4().hex[:12]
    k3d = str(tools / "k3d")
    try:
        run(
            [
                k3d,
                "cluster",
                "create",
                name,
                "--image",
                "rancher/k3s:v1.37.1-k3s1",
                "--servers",
                "1",
                "--agents",
                "0",
                "--no-lb",
                "--no-rollback",
                "--kubeconfig-update-default=false",
                "--kubeconfig-switch-context=false",
                "--k3s-arg",
                "--disable=traefik@server:*",
                "--wait",
                "--timeout",
                "180s",
            ],
        )
        config = run([k3d, "kubeconfig", "get", name])
        (state / "kubeconfig").write_text(config, encoding="utf-8")
        yield
    except (OSError, subprocess.SubprocessError):
        try:
            diagnostics = run(
                ["docker", "logs", "--tail", "200", f"k3d-{name}-server-0"],
                check=False,
                timeout=30,
            )
            print(diagnostics[-20000:], file=sys.stderr, flush=True)
        except (OSError, subprocess.SubprocessError) as error:
            print(
                json.dumps(
                    {"diagnostics_error": type(error).__name__, "cluster": name}
                ),
                file=sys.stderr,
            )
        raise
    finally:
        try:
            result = run([k3d, "cluster", "delete", name], check=False)
            print(result[-10000:], flush=True)
        except (OSError, subprocess.SubprocessError) as error:
            print(
                json.dumps(
                    {"cleanup_error": type(error).__name__, "cluster": name}
                ),
                file=sys.stderr,
            )


def install_ambient(root: Path) -> None:
    """Uses Bazel-fetched charts and private Helm state for concurrent tests."""
    tools = root / "bin"
    run(
        [
            str(tools / "kubectl"),
            "apply",
            "--server-side",
            "--force-conflicts",
            "-f",
            str(root / "charts/gateway-api.yaml"),
        ]
    )
    for release, chart, values in (
        ("istio-base", "base", ()),
        ("istiod", "istiod", ("istiod",)),
        ("istio-cni", "cni", ("cni", "k3d")),
        ("ztunnel", "ztunnel", ("k3d",)),
    ):
        args = [
            str(tools / "helm"),
            "upgrade",
            "--install",
            release,
            str(root / "charts" / (chart + ".tgz")),
            "--namespace",
            "istio-system",
            "--create-namespace",
            "--wait",
            "--timeout",
            "300s",
        ]
        for value in values:
            args.extend(["--values", str(root / "istio" / (value + ".yaml"))])
        run(args, timeout=360)
