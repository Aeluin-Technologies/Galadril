"""Exercises the shipped SSO bridge with real Polis, Hydra, TLS and Envoy."""

from __future__ import annotations

import base64
import io
import json
import secrets
import ssl
import tarfile
import tempfile
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import docker
import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from docker.models.containers import Container, ExecResult
from pydantic import BaseModel, ConfigDict, ValidationError

from infrastructure.auth.tests.webauthn import VirtualAuthenticator
from infrastructure.envoy.testing import identity_files

ROOT = Path(__file__).absolute().parents[3]
HYDRA = "oryd/hydra:v2.3.0@sha256:b94007e19a1f7f78157e7f4ea340da8a55b5f104a0f1198755c256f38ef32b4b"
POLIS = "boxyhq/jackson:26.2.0@sha256:9165f3db9d9f5d3262f23446e860f4929f462d1d9c954af13f3bb44910c3a699"
ENVOY = "envoyproxy/envoy:v1.39.3@sha256:dd85940439de19a0b6ae8419610363ea0ad351d9a994ea007161c206ec1e1865"
NODE = "gcr.io/distroless/nodejs26-debian13@sha256:c85d869757a6a10f68079a87b3ec459d5f951369b7836f9733f9dfd732e5f943"
KRATOS = "oryd/kratos:v26.2.0@sha256:2a13bb8d362c7a7ae33bd7c0f5168aee46921f15c916a06346db91c06dc76643"
ECHO = "traefik/whoami:v1.12.0@sha256:c4717a8d1f0134a7444e24f881160e033991f23027c6c5a9a3f8fd22e70d1d44"
SPICEDB = "authzed/spicedb:v1.56.2@sha256:aa96009a0477f8a759149823407d47ad16d1a74390bae3102b3b0b7143502764"


class NativeSessionFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    error: Literal["AbortError", "TimeoutError", "TypeError", "Error"]
    code: Literal[
        "ECONNREFUSED",
        "ECONNRESET",
        "ETIMEDOUT",
        "UND_ERR_CONNECT_TIMEOUT",
        "UND_ERR_HEADERS_TIMEOUT",
        "UND_ERR_BODY_TIMEOUT",
        "UND_ERR_SOCKET",
        "unknown",
    ]


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("Expected mapping")
    return cast(dict[str, object], value)


def encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def archive(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as bundle:
        for name, body in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(body)
            entry.mode = 0o444
            bundle.addfile(entry, io.BytesIO(body))
    return output.getvalue()


class SsoIntegrationTest(unittest.TestCase):
    origin_host = "127.0.0.1"
    idp_groups = ["Finance"]

    @classmethod
    def setUpClass(cls) -> None:
        cls.docker = docker.from_env()
        cls.addClassCleanup(cls.docker.close)
        cls.containers: list[Container] = []
        cls.addClassCleanup(cls.cleanup_containers)
        cls.directory = tempfile.TemporaryDirectory(prefix="galadril-sso-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.files = identity_files()
        for name, body in cls.files.items():
            target = Path(cls.directory.name) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        cls.context = ssl.create_default_context(
            cadata=cls.files["identity/hydra/ca.pem"].decode()
        )
        cls.start_idp()
        for image in (
            HYDRA,
            POLIS,
            KRATOS,
            ENVOY,
            NODE,
            ECHO,
            SPICEDB,
            "postgres:17.6",
        ):
            cls.docker.images.pull(image)
        cls.database = cls.start(
            "postgres:17.6",
            environment={"POSTGRES_PASSWORD": "test-password"},
            ports={"5432/tcp": ("127.0.0.1", None)},
        )
        cls.wait_database()
        cls.sql("CREATE DATABASE hydra;")
        cls.sql("CREATE DATABASE polis;")
        cls.sql("CREATE DATABASE kratos;")
        cls.sql(
            "CREATE ROLE galadril_auth LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD 'auth-test-password';"
            "CREATE ROLE galadril_app NOLOGIN NOSUPERUSER NOBYPASSRLS;"
        )
        cls.sql(
            (
                ROOT
                / "schemas/postgres/gateway_migrations/202608270001_iam.sql"
            ).read_text()
        )
        cls.sql(
            (
                ROOT
                / "schemas/postgres/gateway_migrations/202610110001_enterprise_sso.sql"
            ).read_text()
        )
        cls.sql(
            (
                ROOT
                / "schemas/postgres/gateway_migrations/202610110002_sso_self_service.sql"
            ).read_text()
        )
        cls.sql(
            "ALTER TABLE iam_users ENABLE ROW LEVEL SECURITY; ALTER TABLE iam_users FORCE ROW LEVEL SECURITY; CREATE POLICY tenant_isolation ON iam_users USING (tenant_id=current_setting('app.tenant_id',true)) WITH CHECK (tenant_id=current_setting('app.tenant_id',true)); GRANT USAGE ON SCHEMA public TO galadril_auth;"
        )
        for name in (
            "202610110003_passkey_mfa.sql",
            "202610110004_organization_profile.sql",
            "202610120001_administration_directory.sql",
            "202610120003_federated_membership.sql",
        ):
            cls.sql(
                (
                    ROOT / "schemas/postgres/gateway_migrations" / name
                ).read_text()
            )
        cls.sql(
            "INSERT INTO sso_tenant_settings(tenant_id,passkey_mfa) VALUES('acme',true),('demo',true);"
        )
        cls.authenticators = {
            "acme": VirtualAuthenticator(),
            "demo": VirtualAuthenticator(),
        }
        cls.database.reload()
        database_ip = cls.database.attrs["NetworkSettings"]["Networks"][
            "bridge"
        ]["IPAddress"]
        cls.proxy = cls.create(
            ENVOY,
            command=[
                "-c",
                "/etc/envoy/config.yaml",
                "--log-level",
                "warn",
                "--concurrency",
                "2",
            ],
            ports={"8080/tcp": ("127.0.0.1", None)},
            extra_hosts={"host.docker.internal": "host-gateway"},
        )
        files = {
            "error-page.lua": (
                ROOT / "infrastructure/envoy/error-page.lua"
            ).read_bytes(),
            "config.yaml": (
                ROOT / "infrastructure/envoy/local.yaml"
            ).read_bytes(),
        }
        for name in ("ca.pem", "cert.pem", "key.pem"):
            files[f"identity/{name}"] = cls.files[f"identity/registry/{name}"]
        for name in ("cert.pem", "key.pem"):
            files[f"public/{name}"] = cls.files[f"identity/gateway/{name}"]
        cls.proxy.put_archive("/etc/envoy", archive(files))
        cls.proxy.start()
        cls.proxy.reload()
        cls.origin = (
            f"https://{cls.origin_host}:"
            + cls.proxy.ports["8080/tcp"][0]["HostPort"]
        )
        namespace = f"container:{cls.proxy.id}"
        cls.start(ECHO, command=["--port", "8081"], network_mode=namespace)
        kratos_env = {
            "DSN": f"postgres://postgres:test-password@{database_ip}:5432/kratos?sslmode=disable",
            "SECRETS_COOKIE": json.dumps([secrets.token_hex(32)]),
            "SECRETS_CIPHER": json.dumps([secrets.token_hex(16)]),
            "SERVE_PUBLIC_BASE_URL": cls.origin + "/identity/",
            "SELFSERVICE_DEFAULT_BROWSER_RETURN_URL": cls.origin
            + "/auth/local/callback",
            "SELFSERVICE_ALLOWED_RETURN_URLS": json.dumps(
                [cls.origin + "/auth/local/callback"]
            ),
            "SELFSERVICE_FLOWS_LOGIN_UI_URL": cls.origin + "/login",
            "SELFSERVICE_FLOWS_ERROR_UI_URL": cls.origin + "/login",
            "SELFSERVICE_FLOWS_LOGIN_AFTER_DEFAULT_BROWSER_RETURN_URL": cls.origin
            + "/auth/local/callback",
            "SELFSERVICE_FLOWS_LOGOUT_AFTER_DEFAULT_BROWSER_RETURN_URL": cls.origin
            + "/login",
        }
        migration = cls.start(
            KRATOS,
            command=["migrate", "sql", "-e", "--yes"],
            environment=kratos_env,
        )
        if migration.wait(timeout=60)["StatusCode"] != 0:
            raise AssertionError(migration.logs().decode())
        kratos = cls.create(
            KRATOS,
            command=["serve", "all", "--config", "/etc/kratos/kratos.yaml"],
            environment=kratos_env,
            network_mode=namespace,
        )
        kratos.put_archive(
            "/etc",
            archive(
                {
                    "kratos/" + name: (
                        ROOT / "infrastructure/auth" / name
                    ).read_bytes()
                    for name in ("kratos.yaml", "identity.schema.json")
                }
            ),
        )
        kratos.start()
        hydra_env = {
            "DSN": f"postgres://postgres:test-password@{database_ip}:5432/hydra?sslmode=disable",
            "SECRETS_SYSTEM": secrets.token_hex(32),
            "URLS_SELF_ISSUER": "https://aeluin.gravitalia.com",
            "URLS_SELF_PUBLIC": cls.origin,
            "URLS_LOGIN": cls.origin + "/auth/login",
            "URLS_CONSENT": cls.origin + "/auth/consent",
            "URLS_ERROR": cls.origin + "/login",
            "TRACING_PROVIDER": "otel",
            "TRACING_PROVIDERS_OTLP_SERVER_URL": "127.0.0.1:4318",
            "TRACING_PROVIDERS_OTLP_INSECURE": "true",
        }
        migration = cls.start(
            HYDRA,
            command=["migrate", "sql", "-e", "--yes"],
            environment=hydra_env,
        )
        if migration.wait(timeout=60)["StatusCode"] != 0:
            raise AssertionError(migration.logs().decode())
        hydra = cls.create(
            HYDRA,
            command=["serve", "all", "--config", "/etc/hydra/hydra.yaml"],
            environment=hydra_env,
            network_mode=namespace,
        )
        hydra.put_archive(
            "/etc",
            archive(
                {
                    "hydra/hydra.yaml": (
                        ROOT / "infrastructure/auth/hydra.yaml"
                    ).read_bytes(),
                    "hydra/tls/cert.pem": cls.files["identity/hydra/cert.pem"],
                    "hydra/tls/key.pem": cls.files["identity/hydra/key.pem"],
                }
            ),
        )
        hydra.start()
        polis = cls.create(
            POLIS,
            environment={
                "HOSTNAME": "127.0.0.1",
                "PORT": "5225",
                "EXTERNAL_URL": cls.origin,
                "DB_ENGINE": "sql",
                "DB_TYPE": "postgres",
                "DB_URL": f"postgres://postgres:test-password@{database_ip}:5432/polis",
                "DB_ENCRYPTION_KEY": secrets.token_hex(16),
                "JACKSON_API_KEYS": "integration-api-key",
                "CLIENT_SECRET_VERIFIER": secrets.token_hex(32),
                "OPENID_REDIRECT_EXACT_MATCH": "true",
                "NODE_EXTRA_CA_CERTS": "/tmp/test-ca.pem",
                "IDP_ENABLED": "false",
                "SSO_TRACES_DISABLE": "true",
            },
            network_mode=namespace,
        )
        polis.put_archive(
            "/tmp", archive({"test-ca.pem": cls.files["identity/hydra/ca.pem"]})
        )
        polis.start()
        cls.polis = polis
        cls.start(
            SPICEDB,
            command=[
                "serve",
                "--grpc-preshared-key=integration-key",
                "--datastore-engine=memory",
                "--http-enabled",
                "--http-addr=0.0.0.0:8443",
            ],
            network_mode=namespace,
        )
        auth_key = secrets.token_hex(32)
        cls.auth_key = auth_key
        persistence = cls.create(
            NODE,
            entrypoint=["/auth_fixture"],
            network_mode=namespace,
            environment={
                "AUTH_DATABASE_URL": f"postgres://galadril_auth:auth-test-password@{database_ip}:5432/postgres",
                "AUTH_GATEWAY_KEY": auth_key,
                "AUTH_SPICEDB_ENDPOINT": "http://127.0.0.1:50051",
            },
        )
        persistence.put_archive(
            "/", (ROOT / "services/gateway/auth_fixture_layer.tar").read_bytes()
        )
        persistence.start()
        studio = cls.create(
            NODE,
            command=["/src/.output/server/index.mjs"],
            environment={
                "HOST": "127.0.0.1",
                "PORT": "3000",
                "NODE_ENV": "production",
                "AUTH_PUBLIC_ORIGIN": cls.origin,
                "AUTH_LOCAL_LOGIN_ENABLED": "true",
                "AUTH_KRATOS_PUBLIC_URL": "http://127.0.0.1:4433",
                "AUTH_KRATOS_ADMIN_URL": "http://127.0.0.1:4434",
                "AUTH_ISSUER": "https://aeluin.gravitalia.com",
                "AUTH_HYDRA_PUBLIC_URL": "https://127.0.0.1:4444",
                "AUTH_HYDRA_ADMIN_URL": "http://127.0.0.1:4445",
                "AUTH_POLIS_URL": "http://127.0.0.1:5225",
                "AUTH_POLIS_API_KEY": "integration-api-key",
                "AUTH_JWKS_URL": "https://127.0.0.1:4444/.well-known/jwks.json",
                "AUTH_CLIENT_SECRET": "integration-studio-secret",
                "AUTH_SESSION_SECRET": secrets.token_hex(32),
                "AUTH_GATEWAY_URL": "http://127.0.0.1:8083/internal/auth/graphql",
                "AUTH_GATEWAY_KEY": auth_key,
                "NODE_EXTRA_CA_CERTS": "/tmp/test-ca.pem",
                "OTEL_TRACES_EXPORTER": "none",
            },
            network_mode=namespace,
        )
        studio.put_archive(
            "/", (ROOT / "front/dashboard/image_layer.tar").read_bytes()
        )
        studio.put_archive(
            "/tmp", archive({"test-ca.pem": cls.files["identity/hydra/ca.pem"]})
        )
        studio.start()
        cls.studio = studio
        cls.wait_http(cls.origin + "/healthz")
        cls.wait_http(cls.origin + "/.well-known/jwks.json")
        cls.connection = cls.private_request(
            "http://127.0.0.1:5225/api/v1/sso",
            {
                "tenant": "acme",
                "product": "galadril",
                "name": "Integration",
                "defaultRedirectUrl": cls.origin + "/auth/polis/callback",
                "redirectUrl": [cls.origin + "/auth/polis/callback"],
                "oidcClientId": "enterprise-client",
                "oidcClientSecret": "enterprise-secret",
                "oidcDiscoveryUrl": cls.idp_origin
                + "/.well-known/openid-configuration",
            },
        )
        identifier = str(cls.connection["clientID"]).replace("'", "''")
        cls.sql(
            f"INSERT INTO sso_connections(domain,tenant_id,connection_id,enabled,jit_provision) VALUES ('acme.example','acme','{identifier}',true,true);"
        )

    @classmethod
    def cleanup_containers(cls) -> None:
        for container in reversed(cls.containers):
            container.remove(force=True)

    @classmethod
    def create(cls, image: str, **kwargs: object) -> Container:
        container = cls.docker.containers.create(image, **kwargs)
        cls.containers.append(container)
        return container

    @classmethod
    def start(cls, image: str, **kwargs: object) -> Container:
        container = cls.create(image, **kwargs)
        container.start()
        return container

    @classmethod
    def sql(cls, sql: str, database: str = "postgres") -> str:
        cls.database.put_archive("/tmp", archive({"query.sql": sql.encode()}))
        result = cls.database.exec_run(
            [
                "psql",
                "-U",
                "postgres",
                "-d",
                database,
                "-v",
                "ON_ERROR_STOP=1",
                "-At",
                "-f",
                "/tmp/query.sql",
            ]
        )
        if result.exit_code:
            raise AssertionError(result.output.decode())
        return result.output.decode().strip()

    @classmethod
    def wait_database(cls) -> None:
        for _attempt in range(60):
            if (
                cls.database.exec_run(
                    ["pg_isready", "-h", "127.0.0.1", "-U", "postgres"]
                ).exit_code
                == 0
            ):
                return
            time.sleep(1)
        raise AssertionError("PostgreSQL readiness timed out")

    @classmethod
    def wait_http(cls, url: str) -> None:
        transport_failure = "none"
        with httpx.Client(verify=cls.context, timeout=2) as client:
            for _attempt in range(90):
                try:
                    if client.get(url).status_code == 200:
                        return
                except httpx.TransportError as error:
                    transport_failure = type(error).__name__
                time.sleep(1)
        logs = "\n".join(
            container.logs(tail=15).decode() for container in cls.containers
        )
        raise AssertionError(
            f"SSO readiness failed (transport={transport_failure}): {logs}"
        )

    @classmethod
    def private_request(
        cls, url: str, body: dict[str, object], method: str = "POST"
    ) -> dict[str, object]:
        script = "fetch(process.argv[1],{method:process.argv[3],headers:{'Content-Type':'application/json','Authorization':'Api-Key integration-api-key','x-polis-return-connection':'true'},body:process.argv[2]}).then(async r=>{console.log(await r.text());process.exit(r.ok?0:1)})"
        for _attempt in range(60):
            result = cls.studio.exec_run(
                [
                    "/nodejs/bin/node",
                    "-e",
                    script,
                    url,
                    json.dumps(body),
                    method,
                ]
            )
            if result.exit_code == 0:
                return mapping(json.loads(result.output))
            time.sleep(1)
        raise AssertionError(result.output.decode())

    def identity_session_status(self, identity_cookie: str) -> int:
        """Check native revocation separately from browser cookie deletion."""
        script = (
            "fetch('http://127.0.0.1:4433/sessions/whoami',"
            "{headers:{cookie:process.argv[1]},redirect:'error',"
            "signal:AbortSignal.timeout(5000)})"
            ".then(async r=>{await r.body?.cancel();console.log(r.status)})"
            ".catch(e=>{const names=['AbortError','TimeoutError','TypeError'];"
            "const codes=['ECONNREFUSED','ECONNRESET','ETIMEDOUT',"
            "'UND_ERR_CONNECT_TIMEOUT','UND_ERR_HEADERS_TIMEOUT',"
            "'UND_ERR_BODY_TIMEOUT','UND_ERR_SOCKET'];"
            "console.log(JSON.stringify({error:names.includes(e.name)?e.name:'Error',"
            "code:codes.includes(e.cause?.code)?e.cause.code:'unknown'}));"
            "process.exitCode=1})"
        )
        response = self.studio.exec_run(
            [
                "/nodejs/bin/node",
                "-e",
                script,
                "__Host-galadril-identity=" + identity_cookie,
            ]
        )
        if response.exit_code != 0:
            try:
                failure = NativeSessionFailure.model_validate_json(
                    response.output
                )
            except ValidationError:
                raise AssertionError(
                    "Native identity session request failed (unknown transport error)"
                ) from None
            self.fail(
                f"Native identity session request failed ({failure.error}, code={failure.code})"
            )
        status = response.output.strip()
        if (
            len(status) != 3
            or not status.isdigit()
            or not 100 <= int(status) <= 599
        ):
            self.fail("Invalid native identity HTTP status")
        return int(status)

    @classmethod
    def start_idp(cls) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = key.public_key().public_numbers()
        jwk = {
            "kty": "RSA",
            "alg": "RS256",
            "use": "sig",
            "kid": "enterprise-test",
            "n": encoded(numbers.n.to_bytes(256, "big")),
            "e": encoded(numbers.e.to_bytes(3, "big")),
        }
        codes: dict[str, str] = {}

        class IdpHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def respond(self, payload: dict[str, object]) -> None:
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                url = urlsplit(self.path)
                if url.path == "/.well-known/openid-configuration":
                    self.respond(
                        {
                            "issuer": cls.idp_origin,
                            "authorization_endpoint": cls.idp_origin
                            + "/authorize",
                            "token_endpoint": cls.idp_origin + "/token",
                            "userinfo_endpoint": cls.idp_origin + "/userinfo",
                            "jwks_uri": cls.idp_origin + "/jwks",
                            "response_types_supported": ["code"],
                            "subject_types_supported": ["public"],
                            "id_token_signing_alg_values_supported": ["RS256"],
                            "token_endpoint_auth_methods_supported": [
                                "client_secret_basic"
                            ],
                            "code_challenge_methods_supported": ["S256"],
                        }
                    )
                elif url.path == "/jwks":
                    self.respond({"keys": [jwk]})
                elif url.path == "/userinfo":
                    self.respond(
                        {
                            "sub": "enterprise-user",
                            "email": "alice@acme.example",
                            "email_verified": True,
                            "given_name": "Alice",
                            "family_name": "Example",
                            "groups": cls.idp_groups,
                        }
                    )
                elif url.path == "/authorize":
                    query = parse_qs(url.query)
                    callback = cls.origin + "/api/oauth/oidc"
                    if (
                        query.get("redirect_uri") != [callback]
                        or len(query.get("nonce", [])) != 1
                        or len(query.get("state", [])) != 1
                    ):
                        self.send_error(400)
                        return
                    code = secrets.token_urlsafe(32)
                    codes[code] = query["nonce"][0]
                    self.send_response(302)
                    self.send_header(
                        "Location",
                        callback
                        + "?"
                        + urlencode({"code": code, "state": query["state"][0]}),
                    )
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self.send_error(404)

            def do_POST(self) -> None:
                if urlsplit(self.path).path == "/userinfo":
                    self.do_GET()
                    return
                if self.headers.get("Transfer-Encoding") == "chunked":
                    chunks = bytearray()
                    while True:
                        size = int(self.rfile.readline().strip(), 16)
                        if size == 0:
                            self.rfile.readline()
                            break
                        chunks.extend(self.rfile.read(size))
                        self.rfile.read(2)
                    data = bytes(chunks)
                else:
                    data = self.rfile.read(
                        int(self.headers.get("Content-Length", "0"))
                    )
                body = parse_qs(data.decode())
                nonce = codes.pop(body.get("code", [""])[0], None)
                if nonce is None:
                    self.send_error(400)
                    return
                claims = {
                    "iss": cls.idp_origin,
                    "aud": "enterprise-client",
                    "sub": "enterprise-user",
                    "iat": int(time.time()),
                    "exp": int(time.time()) + 300,
                    "nonce": nonce,
                }
                unsigned = (
                    encoded(
                        json.dumps(
                            {"alg": "RS256", "kid": "enterprise-test"}
                        ).encode()
                    )
                    + "."
                    + encoded(json.dumps(claims).encode())
                )
                token = (
                    unsigned
                    + "."
                    + encoded(
                        key.sign(
                            unsigned.encode(),
                            padding.PKCS1v15(),
                            hashes.SHA256(),
                        )
                    )
                )
                self.respond(
                    {
                        "access_token": "upstream-test-access",
                        "token_type": "Bearer",
                        "expires_in": 300,
                        "id_token": token,
                    }
                )

        server = ThreadingHTTPServer(("0.0.0.0", 0), IdpHandler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(
            str(Path(cls.directory.name) / "identity/hydra/cert.pem"),
            str(Path(cls.directory.name) / "identity/hydra/key.pem"),
        )
        server.socket = context.wrap_socket(server.socket, server_side=True)
        cls.idp_origin = f"https://host.docker.internal:{server.server_port}"
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        cls.addClassCleanup(server.server_close)
        cls.addClassCleanup(server.shutdown)

    def login(self, client: httpx.Client) -> httpx.Response:
        client.cookies.delete("__Host-galadril-token")
        response = client.post(
            self.origin + "/auth/start",
            headers={"Origin": self.origin},
            data={"email": "alice@acme.example"},
        )
        for _step in range(20):
            if response.status_code == 401:
                return response
            self.assertIn(response.status_code, (302, 303), response.text)
            location = response.headers["location"]
            self.assertNotEqual(location, "/login?continue=1")
            if location == "/login?mfa=1":
                self.assertIsNone(client.cookies.get("__Host-galadril-token"))
                step = client.get(self.origin + "/auth/mfa/options")
                self.assertEqual(step.status_code, 200, step.text)
                proof = self.authenticators["acme"].respond(
                    step.json(), self.origin
                )
                response = client.post(
                    self.origin + "/auth/mfa/verify",
                    headers={"Origin": self.origin},
                    json=proof,
                )
            elif location == "/":
                return client.get(self.origin + "/")
            else:
                location = location.replace(
                    self.idp_origin,
                    self.idp_origin.replace(
                        "host.docker.internal", "127.0.0.1"
                    ),
                )
                response = client.get(location)
        raise AssertionError("Login did not terminate")

    def test_private_gateway_graphql_requires_service_credentials_and_stays_off_public_ingress(
        self,
    ) -> None:
        script = "Promise.all(JSON.parse(process.argv[1]).map(async x=>{const r=await fetch(x.url,{method:'POST',headers:{'content-type':'application/json',...x.headers},body:JSON.stringify(x.body)});return {status:r.status,body:await r.text()}})).then(x=>console.log(JSON.stringify(x)))"
        query = {
            "query": '{loginProvider(email:"probe@demo.invalid",localEnabled:true)}'
        }
        with httpx.Client(verify=self.context, timeout=5) as client:
            response = client.post(
                self.origin + "/internal/auth/graphql", json=query
            )
        self.assertEqual(response.status_code, 404)
        private = "http://127.0.0.1:8083/internal/auth/graphql"
        requests = [
            {"url": private, "headers": {}, "body": query},
            {
                "url": private,
                "headers": {"x-galadril-auth-key": "wrong"},
                "body": query,
            },
            {
                "url": private,
                "headers": {"x-galadril-auth-key": self.auth_key},
                "body": query,
            },
        ]
        result = self.studio.exec_run(
            ["/nodejs/bin/node", "-e", script, json.dumps(requests)]
        )
        self.assertEqual(result.exit_code, 0, result.output.decode())
        responses = json.loads(result.output)
        self.assertEqual(
            [response["status"] for response in responses], [401, 401, 200]
        )
        provider = json.loads(
            json.loads(responses[2]["body"])["data"]["loginProvider"]
        )
        self.assertEqual(provider, {"kind": "local"})

    def test_02_verified_provider_groups_reconcile_only_approved_role_memberships(
        self,
    ) -> None:
        config = json.dumps(
            {
                "connectionId": self.connection["clientID"],
                "enabled": True,
                "mappings": [
                    {"externalGroup": "Finance", "roleName": "finance-reader"}
                ],
            }
        ).replace("'", "''")
        connection = str(self.connection["clientID"]).replace("'", "''")
        self.sql(
            "INSERT INTO iam_roles(tenant_id,role_name) VALUES('acme','finance-reader') ON CONFLICT DO NOTHING"
        )
        self.sql(
            "UPDATE sso_tenant_settings SET permission_import=true WHERE tenant_id='acme'"
        )
        self.sql(
            f"INSERT INTO sso_role_sync_configs(tenant_id,connection_id,configuration) VALUES('acme','{connection}','{config}'::jsonb)"
        )
        try:
            with httpx.Client(
                verify=self.context, timeout=15, follow_redirects=False
            ) as client:
                self.assertEqual(self.login(client).status_code, 200)
                first = client.get(self.origin + "/auth/session").json()
                user = str(first["subject"]).replace("'", "''")
                self.assertEqual(
                    self.sql(
                        f"SELECT count(*) FROM iam_federated_role_assignments WHERE tenant_id='acme' AND user_id='{user}' AND role_name='finance-reader'"
                    ),
                    "1",
                )
                self.assertTrue(self.role_membership(user, "finance-reader"))
                self.__class__.idp_groups = []
                self.assertEqual(self.login(client).status_code, 200)
                self.assertEqual(
                    client.get(self.origin + "/auth/session").json()["subject"],
                    first["subject"],
                )
                self.assertEqual(
                    self.sql(
                        f"SELECT count(*) FROM iam_federated_role_assignments WHERE tenant_id='acme' AND user_id='{user}'"
                    ),
                    "0",
                )
                self.assertFalse(self.role_membership(user, "finance-reader"))
        finally:
            self.__class__.idp_groups = ["Finance"]
            self.sql(
                f"DELETE FROM sso_role_sync_configs WHERE tenant_id='acme' AND connection_id='{connection}'"
            )
            self.sql(
                "UPDATE sso_tenant_settings SET permission_import=false WHERE tenant_id='acme'"
            )

    def role_membership(self, user: str, role: str) -> bool:
        script = "fetch('http://127.0.0.1:8443/v1/permissions/check',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer integration-key'},body:JSON.stringify({consistency:{fullyConsistent:true},resource:{objectType:'role',objectId:'acme/'+process.argv[2]},permission:'member',subject:{object:{objectType:'user',objectId:process.argv[1]}}})}).then(async r=>{console.log(await r.text());process.exit(r.ok?0:1)})"
        response = self.studio.exec_run(
            ["/nodejs/bin/node", "-e", script, user, role]
        )
        self.assertEqual(response.exit_code, 0, response.output.decode())
        return (
            mapping(json.loads(response.output)).get("permissionship")
            == "PERMISSIONSHIP_HAS_PERMISSION"
        )

    def test_broker_secrets_are_encrypted_and_updates_preserve_identity(
        self,
    ) -> None:
        encrypted = self.sql(
            "SELECT count(*) FROM jackson_store WHERE iv IS NOT NULL AND tag IS NOT NULL",
            "polis",
        )
        self.assertGreater(int(encrypted), 0)
        plaintext = self.sql(
            "SELECT count(*) FROM jackson_store WHERE value LIKE '%enterprise-secret%'",
            "polis",
        )
        self.assertEqual(plaintext, "0")
        columns = self.sql(
            "SELECT column_name FROM information_schema.columns WHERE table_name='sso_connections'"
        )
        self.assertNotIn("client_secret", columns)
        updated = self.private_request(
            "http://127.0.0.1:5225/api/v1/sso",
            {
                "clientID": self.connection["clientID"],
                "clientSecret": self.connection["clientSecret"],
                "tenant": "acme",
                "product": "galadril",
                "name": "acme.example",
                "oidcClientId": "enterprise-client",
                "oidcClientSecret": "enterprise-secret",
                "oidcDiscoveryUrl": self.idp_origin
                + "/.well-known/openid-configuration",
            },
            "PATCH",
        )
        self.assertEqual(updated["clientID"], self.connection["clientID"])

    def test_00_broker_resolves_enterprise_discovery(self) -> None:
        """The mock IdP must be reachable from the broker's network namespace."""
        result = self.studio.exec_run(
            [
                "/nodejs/bin/node",
                "-e",
                "fetch(process.argv[1]).then(async r=>{if(!r.ok)throw Error('discovery_http_error');console.log((await r.json()).issuer)}).catch(e=>{console.error(JSON.stringify({code:e.cause?.code||e.code||'discovery_failed'}));process.exit(1)})",
                self.idp_origin + "/.well-known/openid-configuration",
            ]
        )
        self.assertEqual(result.exit_code, 0, result.output.decode())
        self.assertEqual(result.output.decode().strip(), self.idp_origin)

    def test_native_session_probe_releases_the_response_body(self) -> None:
        """A status-only probe must not leave an Undici body pending at process exit."""
        studio = self.studio

        def execute(command: list[str]) -> ExecResult:
            instrumented = command.copy()
            instrumented[2] = (
                "let released=false;process.once('beforeExit',()=>{"
                "if(!released)process.exitCode=1});"
                "globalThis.fetch=async()=>({status:200,"
                "body:{cancel:async()=>{released=true}}});" + instrumented[2]
            )
            return studio.exec_run(instrumented)

        with patch.object(
            type(self), "studio", SimpleNamespace(exec_run=execute)
        ):
            self.assertEqual(
                self.identity_session_status("synthetic-session"), 200
            )

    def test_00_local_account_login_requires_password_and_canonical_binding(
        self,
    ) -> None:
        identity = self.private_request(
            "http://127.0.0.1:4434/admin/identities",
            {
                "schema_id": "local",
                "traits": {"email": "tester@demo.invalid"},
                "metadata_admin": {
                    "galadril": {"tenant_id": "demo", "user_id": "tester"}
                },
                "credentials": {
                    "password": {
                        "config": {
                            "password": "OnlyForThisIntegrationTest-9347!"
                        }
                    }
                },
            },
        )
        self.sql(
            "INSERT INTO iam_users(tenant_id,user_id) VALUES('demo','tester');"
        )
        external = str(identity["id"])
        self.sql(
            f"INSERT INTO iam_external_identities(tenant_id,connection_id,external_subject,user_id) VALUES('demo','kratos','{external}','tester');"
        )
        with httpx.Client(verify=self.context, timeout=10) as client:
            response = client.post(
                self.origin + "/auth/start",
                headers={"Origin": self.origin, "Accept": "application/json"},
                data={"email": "tester@demo.invalid"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["local"])
            response = client.get(response.json()["redirect"])
            for _step in range(25):
                self.assertIn(
                    response.status_code,
                    (302, 303),
                    str(response.request.url.path),
                )
                target = response.headers["location"]
                self.assertNotEqual(target, "/login?continue=1")
                if urlsplit(target).path == "/login" and "flow" in parse_qs(
                    urlsplit(target).query
                ):
                    flow = parse_qs(urlsplit(target).query)["flow"][0]
                    ui_response = client.get(
                        self.origin + "/auth/local/flow", params={"flow": flow}
                    )
                    self.assertEqual(
                        ui_response.status_code, 200, ui_response.text
                    )
                    ui = ui_response.json()
                    fields = {
                        node["attributes"]["name"]: node["attributes"].get(
                            "value", ""
                        )
                        for node in ui["nodes"]
                        if node["attributes"].get("type") == "hidden"
                    }
                    fields.update(
                        identifier="tester@demo.invalid",
                        password="wrong-password",
                        method="password",
                    )
                    rejected = client.post(
                        ui["action"],
                        data=fields,
                        headers={
                            "Origin": self.origin,
                            "Accept": "application/json",
                        },
                    )
                    self.assertEqual(rejected.status_code, 400)
                    self.assertIsNone(
                        client.cookies.get("__Host-galadril-token")
                    )
                    fields["password"] = "OnlyForThisIntegrationTest-9347!"
                    response = client.post(
                        ui["action"],
                        data=fields,
                        headers={"Origin": self.origin},
                    )
                elif target == "/login?mfa=1":
                    self.assertIsNone(
                        client.cookies.get("__Host-galadril-token")
                    )
                    step = client.get(self.origin + "/auth/mfa/options")
                    self.assertEqual(step.status_code, 200, step.text)
                    response = client.post(
                        self.origin + "/auth/mfa/verify",
                        headers={"Origin": self.origin},
                        json=self.authenticators["demo"].respond(
                            step.json(), self.origin
                        ),
                    )
                elif target == "/":
                    break
                else:
                    response = client.get(target)
            else:
                self.fail("Local login did not terminate")
            session = client.get(self.origin + "/auth/session")
            self.assertEqual(session.status_code, 200, session.text)
            self.assertEqual(session.json()["subject"], "tester")
            self.assertEqual(session.json()["tenant"], "demo")
            forwarded = client.post(
                self.origin + "/graphql", headers={"Origin": self.origin}
            )
            self.assertEqual(forwarded.status_code, 200)
            self.assertIn("X-Galadril-Sub: tester", forwarded.text)
            for path in (
                "/identity/admin/identities",
                "/identity/self-service/registration/browser",
                "/identity/sessions/whoami",
            ):
                self.assertEqual(
                    client.get(self.origin + path).status_code, 404
                )
            identity_cookie = client.cookies.get("__Host-galadril-identity")
            self.assertIsInstance(identity_cookie, str)
            self.assertEqual(
                self.identity_session_status(cast(str, identity_cookie)), 200
            )
            logout = client.post(
                self.origin + "/auth/logout",
                headers={"Origin": self.origin, "Accept": "text/html"},
            )
            origin = urlsplit(self.origin)
            for _step in range(4):
                if logout.status_code == 200:
                    break
                self.assertIn(logout.status_code, (302, 303))
                target = urlsplit(
                    urljoin(str(logout.request.url), logout.headers["location"])
                )
                self.assertEqual(
                    (target.scheme, target.netloc),
                    (origin.scheme, origin.netloc),
                    "Native logout must remain on the application origin",
                )
                logout = client.get(
                    target.geturl(), headers={"Accept": "text/html"}
                )
            else:
                self.fail("Native logout exceeded the redirect limit")
            self.assertEqual(logout.status_code, 200)
            self.assertEqual(logout.request.url.path, "/login")
            self.assertEqual(
                client.get(self.origin + "/auth/session").status_code, 401
            )
            self.assertIsNone(client.cookies.get("__Host-galadril-identity"))
            self.assertEqual(
                self.identity_session_status(cast(str, identity_cookie)), 401
            )

    def test_01_sso_migrations_preserve_existing_identities(self) -> None:
        """Reapplying startup DDL must retain established identity ownership."""
        self.sql(
            "INSERT INTO iam_users(tenant_id,user_id) VALUES('migration-replay','existing');"
            "INSERT INTO iam_external_identities(tenant_id,connection_id,external_subject,user_id) "
            "VALUES('migration-replay','enterprise','subject','existing');"
        )
        self.addCleanup(
            self.sql,
            "DELETE FROM iam_external_identities WHERE tenant_id='migration-replay';"
            "DELETE FROM iam_users WHERE tenant_id='migration-replay';",
        )
        for _attempt in range(2):
            for migration in sorted(
                (ROOT / "schemas/postgres/gateway_migrations").glob(
                    "20261011*.sql"
                )
            ):
                self.sql(migration.read_text())
        self.assertEqual(
            self.sql(
                "SELECT user_id FROM iam_external_identities "
                "WHERE tenant_id='migration-replay' AND connection_id='enterprise' "
                "AND external_subject='subject';"
            ),
            "existing",
        )

    def test_enterprise_login_cookie_gateway_and_logout(self) -> None:
        with httpx.Client(verify=self.context, timeout=10) as client:
            login_page = client.get(self.origin + "/login")
            self.assertEqual(login_page.status_code, 200)
            permissions = login_page.headers.get("permissions-policy", "")
            self.assertIn("publickey-credentials-get=(self)", permissions)
            self.assertIn("publickey-credentials-create=(self)", permissions)
            self.assertEqual(client.get(self.origin + "/").status_code, 401)
            self.assertEqual(
                client.get(
                    self.origin + "/", headers={"Accept": "text/html"}
                ).status_code,
                303,
            )
            self.assertEqual(
                client.get(self.origin + "/settings/sso").status_code, 401
            )
            self.assertEqual(
                client.get(
                    self.origin + "/settings/sso",
                    headers={"Accept": "text/html"},
                ).status_code,
                303,
            )
            self.assertEqual(
                client.post(self.origin + "/graphql").status_code, 401
            )
            self.assertEqual(
                client.post(
                    self.origin + "/auth/start",
                    headers={"Origin": "https://evil.example"},
                ).status_code,
                401,
            )
            response = self.login(client)
            self.assertEqual(
                response.status_code,
                200,
                response.text
                + "\nStudio logs:\n"
                + self.studio.logs(tail=10).decode()
                + "\nPolis logs:\n"
                + self.polis.logs(tail=30).decode(),
            )
            session = client.get(self.origin + "/auth/session")
            self.assertEqual(session.status_code, 200, session.text)
            self.assertEqual(session.json()["tenant"], "acme")
            for page in ("/settings/sso", "/builder", "/pengolo"):
                self.assertEqual(
                    client.get(self.origin + page).status_code, 200, page
                )
            self.assertEqual(
                client.post(
                    self.origin + "/auth/permissions/import",
                    headers={"Origin": self.origin},
                    json={"version": 1, "tenant": "acme", "operations": []},
                ).status_code,
                401,
            )
            token = client.cookies.get("__Host-galadril-token")
            self.assertIsNotNone(token)
            cookies = [
                cookie
                for cookie in client.cookies.jar
                if cookie.name == "__Host-galadril-token"
            ]
            self.assertTrue(cookies[0].secure)
            self.assertIn("HttpOnly", cookies[0]._rest)
            self.assertEqual(cookies[0]._rest["SameSite"].lower(), "lax")
            response = client.post(
                self.origin + "/graphql", headers={"Origin": self.origin}
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("X-Galadril-Tenant: acme", response.text)
            self.assertIn(
                "X-Galadril-Sub: " + session.json()["subject"], response.text
            )
            self.assertNotIn("Cookie:", response.text)
            self.assertEqual(
                client.post(
                    self.origin + "/graphql",
                    headers={"Origin": "https://evil.example"},
                ).status_code,
                403,
            )
            self.assertEqual(self.login(client).status_code, 200)
            self.assertEqual(
                self.sql(
                    "SELECT count(*) FROM iam_external_identities WHERE tenant_id='acme';"
                ),
                "1",
            )
            self.sql(
                "UPDATE iam_users SET is_active=false WHERE tenant_id='acme';"
            )
            self.assertEqual(
                client.get(self.origin + "/auth/session").status_code, 401
            )
            self.assertEqual(client.get(self.origin + "/").status_code, 401)
            self.assertEqual(
                client.get(
                    self.origin + "/", headers={"Accept": "text/html"}
                ).status_code,
                303,
            )
            self.assertEqual(self.login(client).status_code, 401)
            self.assertEqual(
                self.sql(
                    "SELECT count(*) FROM iam_users WHERE tenant_id='acme';"
                ),
                "1",
            )
            self.assertEqual(
                self.sql("SELECT count(*) FROM iam_user_roles;"), "0"
            )
            self.assertEqual(
                client.post(
                    self.origin + "/auth/logout",
                    headers={"Origin": self.origin},
                ).status_code,
                303,
            )
            self.assertIsNone(client.cookies.get("__Host-galadril-token"))
            self.assertEqual(
                client.get(self.origin + "/auth/session").status_code, 401
            )
            for _attempt in range(60):
                response = client.post(
                    self.origin + "/auth/start",
                    headers={"Origin": "https://evil.example"},
                )
            self.assertEqual(response.status_code, 429)


if __name__ == "__main__":
    unittest.main()
