# /// script
# requires-python = ">=3.13"
# dependencies = ["httpx==0.28.1"]
# ///
"""Enrolls isolated local test workspaces through private Kratos and Gateway APIs."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import re
import ssl
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from uuid import uuid4

import httpx


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


async def _private_request(
    url: str, body: Mapping[str, object] | None, method: str
) -> object:
    script = "const fs=require('node:fs');const x=JSON.parse(fs.readFileSync(0,'utf8'));fetch(x.url,{method:x.method,redirect:'error',signal:AbortSignal.timeout(20000),headers:{'content-type':'application/json'},body:x.body===null?undefined:JSON.stringify(x.body)}).then(async r=>{console.log(JSON.stringify({status:r.status,body:r.ok?await r.json():null}))}).catch(()=>process.exit(1))"
    process = await asyncio.create_subprocess_exec(
        "docker",
        "compose",
        "--file",
        "infrastructure/docker/docker-compose.yaml",
        "exec",
        "--no-TTY",
        "studio",
        "/nodejs/bin/node",
        "-e",
        script,
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
    if process.returncode:
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
    state_path: Path, recreate_missing: bool
) -> tuple[Mapping[str, object], Mapping[str, object]] | None:
    """Preserves retries unless the operator explicitly replaces a missing identity."""
    if not state_path.exists():
        return None
    state = _mapping(json.loads(state_path.read_text()))
    try:
        identity = _mapping(
            await _private_request(
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
) -> None:
    """Creates no operator-selected tenant IDs and never enables public signup."""
    key = os.environ.get("SSO_PROVISIONING_KEY", "")
    if len(key) < 32 or not origin.startswith("https://"):
        raise ValueError("Set SSO_PROVISIONING_KEY and an HTTPS public origin")
    state_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_path = state_directory / (
        hashlib.sha256(email.encode()).hexdigest() + ".json"
    )
    saved = await saved_identity(state_path, recreate_missing)
    if saved is not None:
        state, identity = saved
    else:
        password = await asyncio.to_thread(
            getpass.getpass, "Test account password (at least 12 characters): "
        )
        identity = _mapping(
            await _private_request(
                "http://127.0.0.1:4434/admin/identities",
                identity_payload(email, password),
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
    await _private_request(
        f"http://127.0.0.1:4434/admin/identities/{identity['id']}",
        bind_identity(identity, organization),
        "PUT",
    )
    print(
        json.dumps(
            {
                "email": email,
                "organization": organization["organization"],
                "login": origin.rstrip("/") + "/login",
            }
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument(
        "--recreate-missing",
        action="store_true",
        help="Operator only: archive stale enrollment state after a confirmed identity 404",
    )
    parser.add_argument(
        "--origin",
        default=os.environ.get("AUTH_PUBLIC_ORIGIN", "https://localhost:8080"),
    )
    parser.add_argument(
        "--ca", type=Path, default=Path(".local/identity/gateway/ca.pem")
    )
    parser.add_argument(
        "--state-directory", type=Path, default=Path(".local/auth/accounts")
    )
    arguments = parser.parse_args()
    try:
        asyncio.run(
            enroll(
                arguments.email,
                arguments.origin,
                arguments.ca,
                arguments.state_directory,
                arguments.recreate_missing,
            )
        )
    except (ValueError, RuntimeError) as error:
        parser.exit(1, f"Local account enrollment failed: {error}\n")
    except httpx.HTTPStatusError as error:
        parser.exit(
            1,
            f"Gateway provisioning returned HTTP {error.response.status_code}; "
            "check service readiness and SSO_PROVISIONING_KEY, then retry with the same state directory.\n",
        )
    except httpx.HTTPError:
        parser.exit(
            1,
            "Local account enrollment failed; check configuration and retry with the same state directory.\n",
        )


if __name__ == "__main__":
    main()
