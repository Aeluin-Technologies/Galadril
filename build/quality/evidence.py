"""Bind Bazel coverage artifacts to the sources and invocation that produced them."""

from hashlib import sha256
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class BazelStart(BaseModel):
    invocation: UUID = Field(validation_alias="uuid")
    command: str
    version: str = Field(validation_alias="buildToolVersion")


class BazelExit(BaseModel):
    code: int = 0


class BazelFinish(BaseModel):
    exit: BazelExit = Field(validation_alias="exitCode")


class BazelEvent(BaseModel):
    started: BazelStart | None = None
    finished: BazelFinish | None = None
    terminal: bool = Field(False, validation_alias="lastMessage")


class CoverageEvidence(BaseModel):
    """Describe the exact report retained from a completed Bazel invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    invocation: UUID
    bazel_version: str
    exit_code: int
    source_sha256: str
    lcov_sha256: str
    execution_roots: tuple[str, ...]


def source_fingerprint(root: Path, paths: list[str]) -> str:
    """Hash source paths and bytes without relying on a clean Git worktree."""

    digest = sha256()
    for path in sorted(paths):
        source = (root / path).read_bytes()
        digest.update(path.encode())
        digest.update(b"\0")
        digest.update(sha256(source).digest())
    return digest.hexdigest()


def capture(
    root: Path,
    paths: list[str],
    raw_lcov: Path,
    events: str,
    before: str,
    artifact: Path,
    execution_roots: tuple[str, ...] = (),
) -> CoverageEvidence:
    """Materialize evidence only after a stable Bazel coverage command finishes."""

    started = None
    finished = None
    terminal = False
    for line in events.splitlines():
        if not line:
            continue
        event = BazelEvent.model_validate_json(line)
        if event.started is not None:
            if started is not None:
                raise ValueError("Multiple Bazel starts in coverage evidence")
            started = event.started
        if event.finished is not None:
            if finished is not None:
                raise ValueError("Multiple Bazel finishes in coverage evidence")
            finished = event.finished
        terminal = event.terminal
    if (
        started is None
        or started.command != "coverage"
        or finished is None
        or not terminal
    ):
        raise ValueError("Expected complete build events for Bazel coverage")

    current = source_fingerprint(root, paths)
    if current != before:
        raise ValueError("Sources changed during Bazel coverage")
    lcov = raw_lcov.read_bytes()
    if not lcov or b"end_of_record" not in lcov:
        raise ValueError("Bazel coverage produced no complete LCOV records")

    receipt = CoverageEvidence(
        invocation=started.invocation,
        bazel_version=started.version,
        exit_code=finished.exit.code,
        source_sha256=current,
        lcov_sha256=sha256(lcov).hexdigest(),
        execution_roots=execution_roots,
    )
    artifact.mkdir(parents=True, exist_ok=True)
    (artifact / "coverage.lcov").write_bytes(lcov)
    (artifact / "receipt.json").write_text(
        receipt.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return receipt


def open_evidence(
    root: Path, paths: list[str], artifact: Path
) -> tuple[CoverageEvidence, str]:
    """Reject mutated or mismatched artifacts before calculating CRAP."""

    receipt = CoverageEvidence.model_validate_json(
        (artifact / "receipt.json").read_text(encoding="utf-8")
    )
    lcov = (artifact / "coverage.lcov").read_bytes()
    if sha256(lcov).hexdigest() != receipt.lcov_sha256:
        raise ValueError(
            "LCOV digest does not match the Bazel coverage receipt"
        )
    if source_fingerprint(root, paths) != receipt.source_sha256:
        raise ValueError("Coverage receipt does not match the current sources")
    return receipt, lcov.decode("utf-8")
