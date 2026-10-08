"""Exercises real Envoy TLS, JWT and workload policies against an echo upstream."""

from __future__ import annotations

import io
import ssl
import tarfile
import tempfile
import time
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import docker
import httpx
import yaml
from docker.models.containers import Container

from infrastructure.envoy.testing import identity_files, jwks_bytes, token

ROOT = Path(__file__).absolute().parents[1]
ENVOY_IMAGE = "envoyproxy/envoy:v1.39.3@sha256:dd85940439de19a0b6ae8419610363ea0ad351d9a994ea007161c206ec1e1865"
ECHO_IMAGE = "traefik/whoami:v1.12.0@sha256:c4717a8d1f0134a7444e24f881160e033991f23027c6c5a9a3f8fd22e70d1d44"
CURL_IMAGE = "curlimages/curl:8.22.0@sha256:58adaa4e8dca9c988bae2aba4ab3434a0bb2da16bbe3f92dec39ec7785166777"


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("Expected a configuration mapping")
    return cast(dict[str, object], value)


def archive(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as bundle:
        for path, content in files.items():
            metadata = tarfile.TarInfo(path)
            metadata.size = len(content)
            metadata.mode = 0o444
            bundle.addfile(metadata, io.BytesIO(content))
    return output.getvalue()


class ProxyIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = docker.from_env()
        cls.addClassCleanup(cls.client.close)
        cls.directory = tempfile.TemporaryDirectory(prefix="envoy-pki-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.files = identity_files()
        for path, content in cls.files.items():
            target = Path(cls.directory.name) / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(0o600)
        cls.client.images.pull(ENVOY_IMAGE)
        cls.client.images.pull(ECHO_IMAGE)
        for profile in ("gateway", "registry", "scribe", "egress", "local"):
            container = cls.client.containers.create(
                ENVOY_IMAGE,
                command=["--mode", "validate", "-c", "/etc/envoy/config.yaml"],
            )
            try:
                cls.install(container, profile, echo=False)
                container.start()
                status = container.wait(timeout=30)
                if status["StatusCode"] != 0:
                    raise AssertionError(container.logs().decode("utf-8"))
            finally:
                container.remove(force=True)

    @classmethod
    def install(
        cls,
        container: Container,
        profile: str,
        *,
        echo: bool,
        upstream_test: bool = False,
        server_identity: str | None = None,
    ) -> None:
        config = (ROOT / f"{profile}.yaml").read_text()
        files = {"config.yaml": config.encode(), "jwks.json": jwks_bytes()}
        for filename in ("ca.pem", "cert.pem", "key.pem"):
            files[f"identity/{filename}"] = cls.files[
                f"identity/{server_identity or ('registry' if profile == 'local' else profile if profile != 'egress' else 'intake')}/{filename}"
            ]
        for filename in ("cert.pem", "key.pem"):
            files[f"public/{filename}"] = cls.files[
                f"identity/gateway/{filename}"
            ]
        if echo:
            parsed = mapping(yaml.safe_load(config))
            clusters = cast(
                list[dict[str, object]],
                mapping(parsed["static_resources"])["clusters"],
            )
            application = next(
                item for item in clusters if item["name"] == "application"
            )
            application.pop("typed_extension_protocol_options", None)
            application["health_checks"] = [
                {
                    "timeout": "1s",
                    "interval": "1s",
                    "unhealthy_threshold": 1,
                    "healthy_threshold": 1,
                    "http_health_check": {"path": "/healthz"},
                }
            ]
            assignment = mapping(application["load_assignment"])
            endpoints = cast(list[dict[str, object]], assignment["endpoints"])
            hosts = cast(
                list[dict[str, object]], next(iter(endpoints))["lb_endpoints"]
            )
            address = mapping(
                mapping(mapping(next(iter(hosts))["endpoint"])["address"])[
                    "socket_address"
                ]
            )
            address["port_value"] = 8081
            if profile == "local":
                listeners = cast(
                    list[dict[str, object]],
                    mapping(parsed["static_resources"])["listeners"],
                )
                health = next(
                    item for item in listeners if item["name"] == "health"
                )
                mapping(mapping(health["address"])["socket_address"])[
                    "address"
                ] = "0.0.0.0"
                for upstream in clusters:
                    if upstream["name"] not in {
                        "registry",
                        "scribe",
                        "gateway_tools",
                    }:
                        continue
                    upstream.pop("typed_extension_protocol_options", None)
                    upstream["health_checks"] = application["health_checks"]
                    assignment = mapping(upstream["load_assignment"])
                    endpoints = cast(
                        list[dict[str, object]], assignment["endpoints"]
                    )
                    hosts = cast(
                        list[dict[str, object]], endpoints[0]["lb_endpoints"]
                    )
                    mapping(
                        mapping(mapping(hosts[0]["endpoint"])["address"])[
                            "socket_address"
                        ]
                    )["port_value"] = 8081
            if upstream_test:
                resources = mapping(parsed["static_resources"])
                listeners = cast(
                    list[dict[str, object]], resources["listeners"]
                )
                if profile == "registry":
                    mapping(
                        mapping(mapping(parsed["admin"])["address"])[
                            "socket_address"
                        ]
                    )["port_value"] = 9903
                    resources["listeners"] = [
                        item for item in listeners if item["name"] != "health"
                    ]
                else:
                    resources["listeners"] = [
                        item
                        for item in listeners
                        if item["name"] != "egress_registry"
                    ]
                    public = next(
                        item for item in listeners if item["name"] == "public"
                    )
                    chains = cast(
                        list[dict[str, object]], public["filter_chains"]
                    )
                    filters = cast(
                        list[dict[str, object]], next(iter(chains))["filters"]
                    )
                    manager = mapping(next(iter(filters))["typed_config"])
                    route_config = mapping(manager["route_config"])
                    hosts_config = cast(
                        list[dict[str, object]], route_config["virtual_hosts"]
                    )
                    routes = cast(
                        list[dict[str, object]],
                        next(iter(hosts_config))["routes"],
                    )
                    action = mapping(next(iter(routes))["route"])
                    action["cluster"] = "registry"
                    action["prefix_rewrite"] = (
                        "/galadril.registry.v1.Registry/GetTenant"
                    )
                    registry = next(
                        item for item in clusters if item["name"] == "registry"
                    )
                    endpoints = cast(
                        list[dict[str, object]],
                        mapping(registry["load_assignment"])["endpoints"],
                    )
                    hosts = cast(
                        list[dict[str, object]],
                        next(iter(endpoints))["lb_endpoints"],
                    )
                    mapping(
                        mapping(
                            mapping(next(iter(hosts))["endpoint"])["address"]
                        )["socket_address"]
                    )["address"] = "127.0.0.1"
            files["config.yaml"] = yaml.safe_dump(
                parsed, sort_keys=False
            ).encode()
        if not container.put_archive("/etc/envoy", archive(files)):
            raise RuntimeError("Envoy fixture upload failed")

    def context(self, identity: str | None = None) -> ssl.SSLContext:
        context = ssl.create_default_context(
            cadata=self.files["identity/gateway/ca.pem"].decode("ascii")
        )
        if identity is not None:
            root = Path(self.directory.name) / "identity" / identity
            context.load_cert_chain(
                str(root / "cert.pem"), str(root / "key.pem")
            )
        return context

    @contextmanager
    def proxy(
        self, profile: str, *, probe_local: bool = False
    ) -> Iterator[dict[int, str]]:
        backend = self.client.containers.run(
            ECHO_IMAGE,
            command=["--port", "8081"],
            detach=True,
            ports={
                f"{port}/tcp": ("127.0.0.1", None)
                for port in (8080, 8443, 50052, 9902)
            },
        )
        proxy = self.client.containers.create(
            ENVOY_IMAGE,
            command=[
                "-c",
                "/etc/envoy/config.yaml",
                "--log-level",
                "warn",
                "--concurrency",
                "2",
            ],
            network_mode=f"container:{backend.id}",
        )
        try:
            self.install(proxy, profile, echo=True)
            proxy.start()
            backend.reload()
            ports = {
                port: f"127.0.0.1:{backend.ports[f'{port}/tcp'][0]['HostPort']}"
                for port in (8080, 8443, 50052, 9902)
            }
            deadline = time.monotonic() + 15
            with httpx.Client(timeout=1) as client:
                while time.monotonic() < deadline:
                    try:
                        if (
                            client.get(
                                f"http://{ports[9902]}/healthz"
                            ).status_code
                            == 200
                        ):
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.1)
                else:
                    self.fail(proxy.logs().decode("utf-8"))
            if probe_local:
                self.client.images.pull(CURL_IMAGE)
                for port, path in (
                    (50054, "/galadril.registry.v1.Registry/GetTenant"),
                    (8092, "/runs"),
                    (8082, "/internal/chat/tools"),
                ):
                    response = self.client.containers.run(
                        CURL_IMAGE,
                        command=[
                            "--silent",
                            "--show-error",
                            "--max-time",
                            "5",
                            "--request",
                            "POST",
                            "--write-out",
                            "\n%{http_code}",
                            f"http://127.0.0.1:{port}{path}",
                        ],
                        network_mode=f"container:{backend.id}",
                        remove=True,
                    ).decode()
                    self.assertTrue(response.rstrip().endswith("200"), response)
            yield ports
        finally:
            proxy.remove(force=True)
            backend.remove(force=True)

    def test_single_proxy_routes_local_apis_and_verifies_public_identity(
        self,
    ) -> None:
        with (
            self.proxy("local", probe_local=True) as ports,
            httpx.Client(verify=self.context(), timeout=5) as client,
        ):
            url = f"https://{ports[8080]}/graphql"
            self.assertEqual(client.post(url).status_code, 401)
            response = client.post(
                url,
                headers={
                    "Authorization": "Bearer " + token(),
                    "x-galadril-sub": "forged",
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("X-Galadril-Sub: user-1", response.text)
            self.assertNotIn("forged", response.text)
            self.assertEqual(
                client.post(
                    f"https://{ports[8080]}/internal/chat/tools",
                    headers={"Authorization": "Bearer " + token()},
                ).status_code,
                404,
            )

    def test_jwt_rejects_invalid_credentials_and_strips_forged_identity(
        self,
    ) -> None:
        with (
            self.proxy("gateway") as ports,
            httpx.Client(verify=self.context(), timeout=5) as client,
        ):
            url = f"https://{ports[8080]}/graphql"
            self.assertEqual(client.post(url).status_code, 401)
            for invalid, status in (
                ("broken", 401),
                (token(exp=1), 401),
                (token(iss="https://attacker"), 401),
                (token(aud="other"), 403),
                (token(nbf=int(time.time()) + 3600), 401),
                (token(exp=None), 401),
            ):
                self.assertEqual(
                    client.post(
                        url, headers={"authorization": f"Bearer {invalid}"}
                    ).status_code,
                    status,
                )
            response = client.post(
                url,
                headers={
                    "authorization": "Bearer " + token(),
                    "x-galadril-sub": "attacker",
                    "x-galadril-role": "admin",
                },
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn("X-Galadril-Sub: user-1", response.text)
            self.assertNotIn("attacker", response.text)
            self.assertNotIn("X-Galadril-Role", response.text)
            self.assertNotIn("Authorization: Bearer", response.text)
            self.assertEqual(
                client.post(
                    f"https://{ports[8080]}/internal/chat/tools",
                    headers={"authorization": "Bearer " + token()},
                ).status_code,
                404,
            )
            self.assertEqual(
                client.get(
                    url,
                    headers={
                        "connection": "upgrade",
                        "upgrade": "websocket",
                        "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ==",
                        "sec-websocket-version": "13",
                    },
                ).status_code,
                401,
            )

    def test_registry_requires_allowed_certificate_and_readers_cannot_mutate(
        self,
    ) -> None:
        with self.proxy("registry") as ports:
            base = f"https://{ports[50052]}/galadril.registry.v1.Registry/"
            for identity in (None, "unknown", "scribe"):
                with httpx.Client(
                    verify=self.context(identity), timeout=5
                ) as client:
                    with self.assertRaises(httpx.TransportError):
                        client.post(base + "GetTenant")
            for identity in ("intake", "vision", "gateway"):
                with httpx.Client(
                    verify=self.context(identity), timeout=5
                ) as client:
                    self.assertEqual(
                        client.post(base + "GetTenant").status_code, 200
                    )
                    self.assertEqual(
                        client.post(base + "PutTenant").status_code,
                        200 if identity == "gateway" else 403,
                    )
                    self.assertEqual(
                        client.post(base + "UnexpectedRpc").status_code, 403
                    )
            with httpx.Client(timeout=5) as client:
                with self.assertRaises(httpx.TransportError):
                    client.post(
                        base.replace("https://", "http://") + "GetTenant"
                    )

    def test_scribe_admits_only_gateway_workload(self) -> None:
        with self.proxy("scribe") as ports:
            url = f"https://{ports[8443]}"
            with httpx.Client(
                verify=self.context("gateway"), timeout=5
            ) as client:
                self.assertEqual(client.post(url + "/runs").status_code, 200)
                self.assertEqual(
                    client.post(url + "/unexpected").status_code, 403
                )
            with httpx.Client(
                verify=self.context("intake"), timeout=5
            ) as client:
                with self.assertRaises(httpx.TransportError):
                    client.post(url + "/runs")

    def test_proxy_to_proxy_mtls_verifies_the_exact_server_identity(
        self,
    ) -> None:
        for identity, expected in (("registry", 200), ("unknown", 503)):
            backend = self.client.containers.run(
                ECHO_IMAGE,
                command=["--port", "8081"],
                detach=True,
                ports={
                    "8080/tcp": ("127.0.0.1", None),
                    "50052/tcp": ("127.0.0.1", None),
                    "9902/tcp": ("127.0.0.1", None),
                },
            )
            containers: list[Container] = []
            try:
                for profile in ("registry", "gateway"):
                    proxy = self.client.containers.create(
                        ENVOY_IMAGE,
                        command=[
                            "-c",
                            "/etc/envoy/config.yaml",
                            "--log-level",
                            "warn",
                            "--concurrency",
                            "2",
                            "--disable-hot-restart",
                        ],
                        network_mode=f"container:{backend.id}",
                    )
                    containers.append(proxy)
                    self.install(
                        proxy,
                        profile,
                        echo=True,
                        upstream_test=True,
                        server_identity=identity
                        if profile == "registry"
                        else None,
                    )
                    proxy.start()
                backend.reload()
                public_port = backend.ports["8080/tcp"][0]["HostPort"]
                registry_port = backend.ports["50052/tcp"][0]["HostPort"]
                with httpx.Client(
                    verify=self.context("gateway"), timeout=2
                ) as client:
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        try:
                            response = client.post(
                                f"https://127.0.0.1:{registry_port}"
                                "/galadril.registry.v1.Registry/GetTenant"
                            )
                            if response.status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        time.sleep(0.1)
                    else:
                        self.fail("Registry proxy did not become ready")
                with httpx.Client(verify=self.context(), timeout=2) as client:
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        try:
                            response = client.post(
                                f"https://127.0.0.1:{public_port}/graphql",
                                headers={"authorization": "Bearer " + token()},
                            )
                            if response.status_code == expected:
                                break
                        except httpx.TransportError:
                            pass
                        time.sleep(0.1)
                    else:
                        self.fail(
                            f"Proxy-to-proxy TLS for {identity} expected {expected}; "
                            f"proxy logs: {[proxy.logs(tail=20).decode('utf-8') for proxy in containers]}"
                        )
                    self.assertEqual(response.status_code, expected)
                    if identity == "unknown":
                        self.assertIn(
                            "remote connection failure", response.text
                        )
            finally:
                for proxy in reversed(containers):
                    proxy.remove(force=True)
                backend.remove(force=True)


if __name__ == "__main__":
    unittest.main()
