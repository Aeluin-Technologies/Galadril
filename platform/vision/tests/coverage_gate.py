"""Runs the complete hermetic Vision unit suite with an 80% coverage gate."""

from __future__ import annotations

from pathlib import Path

import pytest
from coverage import Coverage

_MINIMUM_COVERAGE = 80.0


def _test_paths(root: Path) -> tuple[str, ...]:
    """Returns deterministic test paths while excluding Docker contracts."""
    discovered = [
        path for path in root.rglob("test_*.py") if "security" not in path.parts
    ]
    discovered.extend(
        root / "compute" / name
        for name in ("helpers.py", "loader.py", "tasks.py")
    )
    return tuple(str(path) for path in sorted(discovered))


def main() -> int:
    """Fails when tests regress or aggregate Vision coverage drops below 80%."""
    tests_root = Path(__file__).resolve().parent
    project_root = tests_root.parent
    coverage = Coverage(config_file=str(project_root / "pyproject.toml"))
    coverage.start()
    test_status = pytest.main(
        [*_test_paths(tests_root), "-q", "--import-mode=importlib"]
    )
    coverage.stop()
    coverage.save()
    if test_status is not pytest.ExitCode.OK:
        return int(test_status)
    total = coverage.report()
    return 0 if total >= _MINIMUM_COVERAGE else 2


if __name__ == "__main__":
    raise SystemExit(main())
