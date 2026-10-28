"""Runs the compiled dashboard against the same authorized Gateway and Scribe."""

import asyncio
import io
import json
import os
import tarfile
import uuid
from pathlib import Path

from authentication import browser_cookie_snapshot, login_user
from clients import OUTSIDER_ID, TENANT_ID, UPLOADER_ID
from environment import _run, runfile

from infrastructure.envoy.testing import identity_files

PLAYWRIGHT = "mcr.microsoft.com/playwright:v1.64.1-noble@sha256:47b510dd9230bf0b9bfc9b20102241e577ea7a99334ab4c3ca19f19770e9b8ea"


async def exercise_pengolo_browser(
    prompt: str, search: str, *, administrator: str
) -> None:
    """Keeps browser checks independent of pipeline duration and cookie rotation."""
    for actor in dict.fromkeys((UPLOADER_ID, OUTSIDER_ID, administrator)):
        await login_user(actor, TENANT_ID)
    name = "pengolo-browser-" + uuid.uuid4().hex
    bundle = io.BytesIO()
    files = {
        filename: runfile("tests/e2e/dashboard/" + filename).read_bytes()
        for filename in ("package.json", "package-lock.json", "live.spec.ts")
    }
    files["live-fixture.json"] = json.dumps(
        {
            "cookies": browser_cookie_snapshot(UPLOADER_ID, TENANT_ID),
            "memberCookies": browser_cookie_snapshot(OUTSIDER_ID, TENANT_ID),
            "administratorCookies": browser_cookie_snapshot(
                administrator, TENANT_ID
            ),
            "memberId": OUTSIDER_ID,
            "prompt": prompt,
            "search": search,
        }
    ).encode()
    files["ca.pem"] = identity_files()["identity/gateway/ca.pem"]
    files["playwright.config.ts"] = (
        b'import {defineConfig} from "@playwright/test"; export default defineConfig({testMatch:"live.spec.ts",workers:1,retries:0,timeout:240000,use:{baseURL:"https://127.0.0.1:18080",viewport:{width:1440,height:1000},screenshot:"only-on-failure",trace:"retain-on-failure"},outputDir:"/tmp/results/browser"});'
    )
    with tarfile.open(fileobj=bundle, mode="w") as archive:
        for filename, content in files.items():
            metadata = tarfile.TarInfo("suite/" + filename)
            metadata.size = len(content)
            metadata.mode = 0o600
            archive.addfile(metadata, io.BytesIO(content))
    await _run(
        (
            "docker",
            "create",
            "--name",
            name,
            "--network",
            "host",
            "--shm-size",
            "1g",
            "-e",
            "NODE_EXTRA_CA_CERTS=/tmp/suite/ca.pem",
            PLAYWRIGHT,
            "sleep",
            "infinity",
        )
    )
    try:
        await _run(
            ("docker", "cp", "-", name + ":/tmp"), input_bytes=bundle.getvalue()
        )
        await _run(("docker", "start", name))
        await _run(
            (
                "docker",
                "exec",
                "-w",
                "/tmp/suite",
                name,
                "bash",
                "-euc",
                "command -v certutil >/dev/null || (apt-get update -qq && apt-get install -y -qq --no-install-recommends libnss3-tools)\nmkdir -p /root/.pki/nssdb /tmp/results\ncertutil -N -d sql:/root/.pki/nssdb --empty-password\ncertutil -A -d sql:/root/.pki/nssdb -n galadril-test -t 'C,,' -i ca.pem\nnpm ci --ignore-scripts --no-audit --no-fund",
            ),
            timeout_seconds=180,
        )
        try:
            print(
                await _run(
                    (
                        "docker",
                        "exec",
                        "-w",
                        "/tmp/suite",
                        name,
                        "npx",
                        "--no-install",
                        "playwright",
                        "test",
                    ),
                    timeout_seconds=300,
                ),
                flush=True,
            )
        finally:
            process = await asyncio.create_subprocess_exec(
                "docker",
                "cp",
                name + ":/tmp/results",
                "-",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            content, error = await asyncio.wait_for(process.communicate(), 30)
            if process.returncode != 0:
                raise RuntimeError(
                    "Browser artifact export failed: " + error.decode()
                )
            with tarfile.open(fileobj=io.BytesIO(content)) as archive:
                archive.extractall(
                    Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"]),
                    filter="data",
                )
    finally:
        await _run(("docker", "rm", "--force", name), check=False)
