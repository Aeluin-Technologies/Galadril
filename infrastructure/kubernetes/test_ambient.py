"""Exercises shipped Ambient policies on an explicitly isolated test cluster."""

from __future__ import annotations

import base64
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import cast

import httpx
import yaml

from infrastructure.envoy.testing import identity_files, jwks_bytes, token

ROOT = Path(__file__).absolute().parent
ECHO_IMAGE = "traefik/whoami:v1.12.0@sha256:c4717a8d1f0134a7444e24f881160e033991f23027c6c5a9a3f8fd22e70d1d44"
CURL_IMAGE = "curlimages/curl:8.22.0@sha256:58adaa4e8dca9c988bae2aba4ab3434a0bb2da16bbe3f92dec39ec7785166777"


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("Expected Kubernetes object")
    return cast(dict[str, object], value)


def kubectl(*args: str, payload: str | None = None, check: bool = True) -> str:
    """Keeps cluster operations bounded and exposes the primary failure."""
    result = subprocess.run(
        ["kubectl", *args],
        input=payload,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=210,
        check=False,
    )
    if check and result.returncode:
        raise RuntimeError(f"kubectl {args} failed: {result.stdout[-10000:]}")
    return result.stdout


class AmbientIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if os.environ.get("GALADRIL_MESH_E2E") != "1":
            raise RuntimeError(
                "Set GALADRIL_MESH_E2E=1 on an isolated test cluster"
            )
        namespaces = mapping(
            json.loads(kubectl("get", "namespaces", "-o", "json"))
        )
        items = cast(list[dict[str, object]], namespaces["items"])
        if any(
            mapping(item["metadata"])["name"]
            in {"galadril", "galadril-plaintext"}
            for item in items
        ):
            raise RuntimeError(
                "Refusing to replace an existing galadril namespace"
            )
        with tempfile.TemporaryDirectory() as workspace:
            target = Path(workspace)
            for relative in (
                "infrastructure/kubernetes",
                "schemas/avro",
                "schemas/spicedb",
            ):
                source = ROOT.parents[1] / relative
                destination = target / relative
                destination.mkdir(parents=True)
                for file in source.iterdir():
                    if file.is_file() and file.suffix in {
                        ".yaml",
                        ".json",
                        ".avsc",
                        ".zed",
                    }:
                        shutil.copyfile(file, destination / file.name)
            rendered = kubectl(
                "kustomize", str(target / "infrastructure/kubernetes")
            )
        documents: list[dict[str, object]] = []
        for raw in yaml.safe_load_all(rendered):
            document = mapping(raw)
            kind = document["kind"]
            if kind in {"Job", "Telemetry"} or (
                kind == "ConfigMap"
                and mapping(document["metadata"])["name"] != "proxy-defaults"
            ):
                continue
            if kind == "Deployment":
                name = str(mapping(document["metadata"])["name"])
                if name not in {"gateway", "registry", "scribe"}:
                    continue
                port = {"gateway": 8081, "registry": 50053, "scribe": 8091}[
                    name
                ]
                pod = mapping(
                    mapping(mapping(document["spec"])["template"])["spec"]
                )
                pod.pop("initContainers", None)
                pod.pop("volumes", None)
                pod["containers"] = [
                    {
                        "name": name,
                        "image": ECHO_IMAGE,
                        "args": [f"--port={port}"],
                        "securityContext": {
                            "runAsNonRoot": True,
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]},
                            "readOnlyRootFilesystem": True,
                        },
                    }
                ]
            if kind == "Service":
                # The echo fixture speaks HTTP; authorization sees the same RPC paths.
                ports = cast(
                    list[dict[str, object]], mapping(document["spec"])["ports"]
                )
                for port in ports:
                    port["name"] = "http"
                    port["appProtocol"] = "http"
            if kind == "RequestAuthentication":
                rules = cast(
                    list[dict[str, object]],
                    mapping(document["spec"])["jwtRules"],
                )
                rule = next(iter(rules))
                rule["jwks"] = jwks_bytes().decode()
            if kind == "EnvoyFilter":
                patches = cast(
                    list[dict[str, object]],
                    mapping(document["spec"])["configPatches"],
                )
                typed = mapping(
                    mapping(mapping(patches[1]["patch"])["value"])[
                        "typed_config"
                    ]
                )
                provider = mapping(mapping(typed["providers"])["origins-0"])
                mapping(provider["local_jwks"])["inline_string"] = (
                    jwks_bytes().decode()
                )
            documents.append(document)
        namespace = next(
            item for item in documents if item["kind"] == "Namespace"
        )
        kubectl("apply", "-f", "-", payload=yaml.safe_dump(namespace))
        cls.addClassCleanup(
            kubectl, "delete", "namespace", "galadril", "--wait=false"
        )
        files = identity_files()
        documents.append(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": "galadril-public-tls",
                    "namespace": "galadril",
                },
                "type": "kubernetes.io/tls",
                "data": {
                    "tls.crt": base64.b64encode(
                        files["identity/gateway/cert.pem"]
                    ).decode(),
                    "tls.key": base64.b64encode(
                        files["identity/gateway/key.pem"]
                    ).decode(),
                },
            }
        )
        for name in ("gateway", "intake", "vision", "scribe", "unknown"):
            documents.append(
                {
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "metadata": {
                        "name": f"caller-{name}",
                        "namespace": "galadril",
                    },
                    "spec": {
                        "serviceAccountName": "default"
                        if name == "unknown"
                        else name,
                        "automountServiceAccountToken": False,
                        "securityContext": {
                            "runAsUser": 1000,
                            "runAsNonRoot": True,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": "curl",
                                "image": CURL_IMAGE,
                                "command": ["sleep", "3600"],
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                            }
                        ],
                    },
                }
            )
        documents.append(
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": "galadril-plaintext"},
            }
        )
        plain = json.loads(json.dumps(documents[-2]))
        plain["metadata"] = {
            "name": "caller-plain",
            "namespace": "galadril-plaintext",
        }
        plain["spec"]["serviceAccountName"] = "default"
        documents.append(plain)
        cls.addClassCleanup(
            kubectl, "delete", "namespace", "galadril-plaintext", "--wait=false"
        )
        kubectl("apply", "-f", "-", payload=yaml.safe_dump_all(documents))
        kubectl(
            "wait",
            "--namespace",
            "galadril",
            "--for=condition=Ready",
            "pods",
            "--all",
            "--timeout=180s",
        )
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.log = open(Path(cls.directory.name) / "port-forward.log", "w+")
        cls.addClassCleanup(cls.log.close)
        cls.forward = subprocess.Popen(
            [
                "kubectl",
                "-n",
                "galadril",
                "port-forward",
                "service/public-gateway-istio",
                "18443:443",
            ],
            stdout=cls.log,
            stderr=subprocess.STDOUT,
        )
        cls.addClassCleanup(cls.stop_forward)
        cls.client = httpx.Client(
            base_url="https://127.0.0.1:18443",
            verify=ssl.create_default_context(
                cadata=files["identity/gateway/ca.pem"].decode()
            ),
            timeout=10,
        )
        cls.addClassCleanup(cls.client.close)
        status = "no response"
        for _attempt in range(60):
            try:
                response = cls.client.post(
                    "/graphql", headers={"Authorization": "Bearer " + token()}
                )
                status = f"{response.status_code} {response.text}"
                if response.status_code == 200:
                    break
            except httpx.TransportError:
                # Port-forward and ingress listeners may still be starting.
                pass
            if cls.forward.poll() is not None:
                raise RuntimeError("Ingress port-forward exited")
            time.sleep(1)
        else:
            raise RuntimeError(f"Ingress did not become ready: {status}")

    @classmethod
    def stop_forward(cls) -> None:
        cls.forward.terminate()
        try:
            cls.forward.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.forward.kill()
            cls.forward.wait(timeout=5)

    def test_public_tls_rejects_obsolete_versions(self) -> None:
        context = ssl.create_default_context(
            cadata=identity_files()["identity/gateway/ca.pem"].decode()
        )
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        with httpx.Client(verify=context, timeout=5) as client:
            with self.assertRaises(httpx.TransportError):
                client.post("https://127.0.0.1:18443/graphql")

    def test_unenrolled_plaintext_cannot_reach_an_api(self) -> None:
        kubectl(
            "wait",
            "-n",
            "galadril-plaintext",
            "--for=condition=Ready",
            "pod/caller-plain",
            "--timeout=120s",
        )
        response = kubectl(
            "-n",
            "galadril-plaintext",
            "exec",
            "caller-plain",
            "--",
            "curl",
            "--silent",
            "--show-error",
            "--max-time",
            "5",
            "--request",
            "POST",
            "--write-out",
            "\n%{http_code}",
            "http://registry.galadril.svc.cluster.local:50053/galadril.registry.v1.Registry/GetRuntimePipeline",
            check=False,
        )
        self.assertIn("000", response)

    def test_public_jwt_and_header_integrity(self) -> None:
        for signed in (
            "malformed",
            token(exp=1),
            token(exp=None),
            token(aud="other"),
            token(iss="other"),
        ):
            with self.subTest(token=signed[:12]):
                self.assertIn(
                    self.client.post(
                        "/graphql",
                        headers={"Authorization": "Bearer " + signed},
                    ).status_code,
                    (401, 403),
                )
        self.assertEqual(self.client.post("/graphql").status_code, 403)
        response = self.client.post(
            "/graphql",
            headers={
                "Authorization": "Bearer " + token(),
                "x-galadril-sub": "forged",
                "x-galadril-role": "admin",
                "x-forwarded-client-cert": "forged",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("X-Galadril-Sub: user-1", response.text)
        self.assertNotIn("forged", response.text)
        self.assertNotIn("X-Galadril-Role: admin", response.text)
        self.assertNotIn("Authorization: Bearer", response.text)
        self.assertIn(
            self.client.post(
                "/internal/chat/tools",
                headers={"Authorization": "Bearer " + token()},
            ).status_code,
            (403, 404),
        )

    def request(self, caller: str, address: str, path: str) -> str:
        return kubectl(
            "-n",
            "galadril",
            "exec",
            f"caller-{caller}",
            "--",
            "curl",
            "--silent",
            "--show-error",
            "--max-time",
            "10",
            "--request",
            "POST",
            "--write-out",
            "\n%{http_code}",
            f"http://{address}{path}",
            check=False,
        )

    def test_workload_identity_and_rpc_permissions(self) -> None:
        read = "/galadril.registry.v1.Registry/GetRuntimePipeline"
        write = "/galadril.registry.v1.Registry/PutPipeline"
        for caller in ("gateway", "intake", "vision"):
            self.assertTrue(
                self.request(caller, "registry:50053", read)
                .rstrip()
                .endswith("200")
            )
        self.assertTrue(
            self.request("gateway", "registry:50053", write)
            .rstrip()
            .endswith("200")
        )
        for caller in ("intake", "vision", "scribe", "unknown"):
            self.assertTrue(
                self.request(caller, "registry:50053", write)
                .rstrip()
                .endswith("403")
            )
        self.assertTrue(
            self.request("gateway", "scribe:8091", "/runs")
            .rstrip()
            .endswith("200")
        )
        self.assertTrue(
            self.request("intake", "scribe:8091", "/runs")
            .rstrip()
            .endswith("403")
        )
        self.assertTrue(
            self.request("scribe", "gateway:8081", "/internal/chat/tools")
            .rstrip()
            .endswith("200")
        )
        self.assertTrue(
            self.request("gateway", "gateway:8081", "/graphql")
            .rstrip()
            .endswith("403")
        )

    def test_direct_pod_addresses_cannot_bypass_waypoint(self) -> None:
        address = kubectl(
            "-n",
            "galadril",
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/name=registry",
            "-o",
            "jsonpath={.items[0].status.podIP}",
        ).strip()
        for caller in ("gateway", "intake", "unknown"):
            response = self.request(
                caller,
                f"{address}:50053",
                "/galadril.registry.v1.Registry/PutPipeline",
            )
            if caller == "gateway":
                self.assertTrue(response.rstrip().endswith("200"), response)
            else:
                self.assertFalse(response.rstrip().endswith("200"), response)


if __name__ == "__main__":
    unittest.main()
