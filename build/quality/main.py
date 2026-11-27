"""Run workspace-wide quality checks against an explicit Git and Bazel input set."""

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from check import LANGUAGES, Finding, architecture_violations
from evidence import capture, open_evidence, source_fingerprint
from metrics import findings_for, measure, summarize
from tools import tool_command

# These sources are generated or vendored. Keep their producers and contract tests
# under the same checks as other first-party code.
GENERATED = frozenset(
    {
        "platform/document-compiler/src/generated/galadril.document_compiler.v1.rs",
        "platform/ontology/rust/wire/src/block_generated.rs",
        "platform/registry/python/galadril_registry_api/registry_pb2.py",
        "platform/registry/python/galadril_registry_api/registry_pb2.pyi",
        "platform/registry/src/generated/galadril.registry.v1.rs",
        "platform/telemetry/src/generated/contract.rs",
    }
)

NEGATIVE_FIXTURES = frozenset(
    {
        "build/quality/fixtures/documentation_violation.rs",
        "build/quality/fixtures/format_violation.rs",
        "build/quality/fixtures/lint_violation.rs",
        "build/quality/fixtures/type_violation.rs",
    }
)


def source_paths(root: Path) -> list[str]:
    """Collect first-party sources, including untracked additions during iteration."""

    result = subprocess.run(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    )
    paths = {os.fsdecode(name) for name in result.stdout.split(b"\0") if name}
    return sorted(
        path
        for path in paths
        if Path(path).suffix in {*LANGUAGES, ".pyi"}
        and path not in GENERATED | NEGATIVE_FIXTURES
        and not path.startswith("third_party/hipporag/upstream/")
        and (root / path).is_file()
    )


def print_findings(findings: list[Finding]) -> bool:
    """Emit deterministic compiler-style diagnostics and detect blocking findings."""

    for finding in findings:
        print(
            f"{finding.path}:{finding.line}: {finding.rule}: {finding.detail}"
        )
    return any(finding.rule != "crap-target" for finding in findings)


def _python_command(root: Path, module: str, args: list[str]) -> int:
    return subprocess.run(
        [*tool_command(module), *args], cwd=root, check=False
    ).returncode


def check_python(root: Path, paths: list[str]) -> bool:
    """Run all Python analyzers with locked tools and retain every diagnostic."""

    python_paths = [path for path in paths if path.endswith((".py", ".pyi"))]
    if not python_paths:
        raise ValueError("No Python sources in the engineering check")

    statuses = [
        _python_command(
            root, "ruff", ["format", "--no-cache", "--check", *python_paths]
        ),
        _python_command(root, "ruff", ["check", "--no-cache", *python_paths]),
        _python_command(
            root,
            "basedpyright",
            [
                "--project",
                "pyproject.toml",
                "--pythonpath",
                sys.executable,
                *python_paths,
            ],
        ),
    ]
    return any(status != 0 for status in statuses)


class Arguments(argparse.Namespace):
    """Provide explicit types for validated command-line arguments."""

    check: str = ""
    lcov: Path | None = None
    artifact: Path | None = None
    events: Path | None = None
    before: str | None = None
    execution_root: list[str]
    output: Path | None = None


def main() -> int:
    """Require a checked-out workspace and a Bazel LCOV report for CRAP."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "check",
        choices=["architecture", "python", "fingerprint", "capture", "crap"],
    )
    parser.add_argument("--lcov", type=Path)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--events", type=Path)
    parser.add_argument("--before")
    parser.add_argument("--execution-root", action="append", default=[])
    parser.add_argument("--output", type=Path)
    args = Arguments()
    parser.parse_args(namespace=args)
    root = Path(os.environ["BUILD_WORKSPACE_DIRECTORY"]).resolve()
    paths = source_paths(root)
    if not paths:
        parser.error("No first-party source files in the workspace")

    if args.check == "python":
        return int(check_python(root, paths))
    if args.check == "architecture":
        return int(
            print_findings(
                [
                    finding
                    for path in paths
                    for finding in architecture_violations(
                        path, (root / path).read_text(encoding="utf-8")
                    )
                ]
            )
        )

    if args.check == "fingerprint":
        print(source_fingerprint(root, paths))
        return 0
    if args.artifact is None:
        parser.error(
            f"{args.check} requires --artifact from the Bazel coverage runner"
        )
    if args.check == "capture":
        if args.lcov is None or args.events is None or args.before is None:
            parser.error(
                "capture requires the Bazel LCOV, BEP events and source fingerprint"
            )
        capture(
            root,
            paths,
            root / args.lcov,
            (root / args.events).read_text(encoding="utf-8"),
            args.before,
            root / args.artifact,
            tuple(args.execution_root),
        )
        return 0
    if args.lcov is not None:
        parser.error(
            "CRAP consumes a verified --artifact, containing Bazel LCOV and a receipt"
        )

    receipt, lcov = open_evidence(root, paths, root / args.artifact)
    roots = (root, *(Path(path) for path in receipt.execution_roots))
    metrics = measure(root, paths, lcov, roots)
    if args.output is not None:
        report = {
            "evidence": receipt.model_dump(mode="json"),
            "summary": summarize(metrics),
            "functions": [asdict(metric) for metric in metrics],
        }
        (root / args.output).write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return int(print_findings(findings_for(metrics)) or receipt.exit_code != 0)


if __name__ == "__main__":
    raise SystemExit(main())
