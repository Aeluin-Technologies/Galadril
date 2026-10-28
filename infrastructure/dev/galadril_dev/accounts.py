"""Enrolls isolated local test workspaces through private Kratos and Gateway APIs."""

from __future__ import annotations

import asyncio
import getpass
import hashlib
import json
import os
import re
import ssl
from collections.abc import Awaitable, Callable, Mapping
from functools import partial
from pathlib import Path
from typing import cast
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr


class IdentityApiError(RuntimeError):
    """Reports identity status without exposing credentials or response payloads."""

    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"Private identity API returned HTTP {status}")


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Expected an API object")
    return cast(Mapping[str, object], value)


def identity_payload(email: str, password: str) -> dict[str, object]:
    """Keeps password hashing entirely inside Kratos."""
    if len(password) < 12 or len(email) > 254 or "@" not in email:
        raise ValueError(
            "Provide a valid email and a password of at least 12 characters"
        )
    return {
        "schema_id": "local",
        "traits": {"email": email},
        "credentials": {"password": {"config": {"password": password}}},
    }


def bind_identity(
    identity: Mapping[str, object], organization: Mapping[str, object]
) -> dict[str, object]:
    """Uses canonical identifiers returned by authenticated Gateway enrollment."""
    tenant, owner = organization.get("organization"), organization.get("owner")
    if (
        not isinstance(tenant, str)
        or not isinstance(owner, str)
        or not all(
            re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", value)
            for value in (tenant, owner)
        )
    ):
        raise ValueError("Invalid enrollment references")
    return {
        "schema_id": identity["schema_id"],
        "state": identity["state"],
        "traits": identity["traits"],
        "metadata_admin": {"galadril": {"tenant_id": tenant, "user_id": owner}},
    }


class Enrollment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    email: str
    organization: str
    owner: str
    identity: str
    login: str


PrivateRequest = Callable[
    [str, Mapping[str, object] | None, str], Awaitable[object]
]


async def _private_request(
    url: str,
    body: Mapping[str, object] | None,
    method: str,
    *,
    compose_command: tuple[str, ...] | None = None,
    environment: Mapping[str, str] | None = None,
    root: Path | None = None,
) -> object:
    if compose_command is None:
        raise ValueError("The managed Compose command is required")
    script = "const fs=require('node:fs');const x=JSON.parse(fs.readFileSync(0,'utf8'));fetch(x.url,{method:x.method,redirect:'error',signal:AbortSignal.timeout(20000),headers:{'content-type':'application/json'},body:x.body===null?undefined:JSON.stringify(x.body)}).then(async r=>{console.log(JSON.stringify({status:r.status,body:r.ok?await r.json():null}))}).catch(()=>process.exit(1))"
    output = b""
    for executable in ("/nodejs/bin/node", "/usr/local/bin/node"):
        process = await asyncio.create_subprocess_exec(
            *compose_command,
            "exec",
            "--no-TTY",
            "studio",
            executable,
            "-e",
            script,
            cwd=root,
            env={**os.environ, **(environment or {})},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            output, _stderr = await asyncio.wait_for(
                process.communicate(
                    json.dumps(
                        {"url": url, "body": body, "method": method}
                    ).encode()
                ),
                30,
            )
        except TimeoutError:
            process.kill()
            await process.communicate()
            raise RuntimeError("Private identity API timed out") from None
        if process.returncode == 0:
            break
        # Exit 127 proves Node never ran; retrying any other POST could duplicate enrollment.
        if process.returncode != 127 or executable == "/usr/local/bin/node":
            raise RuntimeError(
                "Private identity API unavailable; check the local-auth profile"
            )
    response = _mapping(json.loads(output))
    status = response.get("status")
    if not isinstance(status, int):
        raise RuntimeError("Invalid private identity API response")
    if not 200 <= status < 300:
        raise IdentityApiError(status)
    return response.get("body")


async def saved_identity(
    state_path: Path,
    recreate_missing: bool,
    request: PrivateRequest | None = None,
) -> tuple[Mapping[str, object], Mapping[str, object]] | None:
    """Preserves retries unless the operator explicitly replaces a missing identity."""
    if request is None:
        request = _private_request
    if not state_path.exists():
        return None
    state = _mapping(json.loads(state_path.read_text()))
    try:
        identity = _mapping(
            await request(
                f"http://127.0.0.1:4434/admin/identities/{state['identity']}",
                None,
                "GET",
            )
        )
    except IdentityApiError as error:
        if error.status != 404:
            raise
        if not recreate_missing:
            raise ValueError(
                "Saved identity no longer exists. After resetting the local databases, "
                "the deployment operator can rerun with --recreate-missing."
            ) from None
        state_path.rename(
            state_path.with_name(f"{state_path.name}.previous-{uuid4()}")
        )
        return None
    return state, identity


async def enroll(
    email: str,
    origin: str,
    ca: Path,
    state_directory: Path,
    recreate_missing: bool = False,
    *,
    compose_command: tuple[str, ...] | None = None,
    environment: Mapping[str, str] | None = None,
    root: Path | None = None,
    password: SecretStr | None = None,
) -> Enrollment:
    """Creates no operator-selected tenant IDs and never enables public signup."""
    env = environment if environment is not None else os.environ
    key = env.get("SSO_PROVISIONING_KEY", "")
    if len(key) < 32 or not origin.startswith("https://"):
        raise ValueError("Set SSO_PROVISIONING_KEY and an HTTPS public origin")
    if compose_command is None:
        raise ValueError("The managed Compose command is required")
    parsed = httpx.URL(origin)
    if (
        parsed.host not in {"localhost", "127.0.0.1", "::1"}
        or parsed.userinfo
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Local enrollment requires a verified loopback HTTPS origin"
        )
    request: PrivateRequest = partial(
        _private_request,
        compose_command=compose_command,
        environment=env,
        root=root,
    )
    state_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_path = state_directory / (
        hashlib.sha256(email.encode()).hexdigest() + ".json"
    )
    saved = await saved_identity(state_path, recreate_missing, request)
    if saved is not None:
        state, identity = saved
    else:
        entered = (
            password.get_secret_value()
            if password is not None
            else await asyncio.to_thread(
                getpass.getpass,
                "Test account password (at least 12 characters): ",
            )
        )
        identity = _mapping(
            await request(
                "http://127.0.0.1:4434/admin/identities",
                identity_payload(email, entered),
                "POST",
            )
        )
        state = {"request_id": str(uuid4()), "identity": identity["id"]}
        descriptor = os.open(
            state_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(descriptor, "w") as output:
            json.dump(state, output)
    async with httpx.AsyncClient(
        verify=ssl.create_default_context(cafile=str(ca)),
        timeout=60,
        follow_redirects=False,
    ) as client:
        response = await client.post(
            origin.rstrip("/") + "/provisioning/local-organizations",
            headers={"X-Galadril-Provisioning-Key": key},
            json={
                "request_id": state["request_id"],
                "owner_external_subject": identity["id"],
            },
        )
        response.raise_for_status()
        organization = _mapping(response.json())
    await request(
        f"http://127.0.0.1:4434/admin/identities/{identity['id']}",
        bind_identity(identity, organization),
        "PUT",
    )
    return Enrollment(
        email=email,
        organization=str(organization["organization"]),
        owner=str(organization["owner"]),
        identity=str(identity["id"]),
        login=origin.rstrip("/") + "/login",
    )
