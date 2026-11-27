"""Runs pinned Chromium against real services with ephemeral fixture sessions."""

import asyncio
import io
import json
import os
import tarfile
import uuid
from collections.abc import Mapping
from pathlib import Path

from environment import _run, runfile

from infrastructure.envoy.testing import identity_files

PLAYWRIGHT = "mcr.microsoft.com/playwright:v1.64.1-noble@sha256:47b510dd9230bf0b9bfc9b20102241e577ea7a99334ab4c3ca19f19770e9b8ea"


async def run_browser_suite(spec: str, fixture: Mapping[str, object]) -> None:
    """Shares the real-session runner without proxying or replacing application calls."""
    if spec not in {"live.spec.ts", "gol-live.spec.ts"}:
        raise ValueError("Unknown browser scenario")
    name = "galadril-browser-" + uuid.uuid4().hex
    bundle = io.BytesIO()
    helpers = ("wire.ts",) if spec == "live.spec.ts" else ()
    files = {
        filename: runfile("tests/e2e/dashboard/" + filename).read_bytes()
        for filename in ("package.json", "package-lock.json", spec, *helpers)
    }
    files["live-fixture.json"] = json.dumps(fixture).encode()
    files["ca.pem"] = identity_files()["identity/gateway/ca.pem"]
    files["playwright.config.ts"] = (
        'import {defineConfig} from "@playwright/test"; export default defineConfig({testMatch:'
        + json.dumps(spec)
        + ',workers:1,retries:0,timeout:240000,use:{baseURL:"https://127.0.0.1:18080",viewport:{width:1440,height:1000},screenshot:"only-on-failure",trace:"retain-on-failure"},outputDir:"/tmp/results/browser"});'
    ).encode()
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
