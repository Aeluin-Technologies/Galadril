"""Protects unit coverage collection from Docker integration dependencies."""

from __future__ import annotations

from pathlib import Path

import pytest
from coverage_gate import _test_paths


def test_unit_collection_excludes_docker_contracts(tmp_path: Path) -> None:
    unit = tmp_path / "postgres" / "test_graph.py"
    integration = tmp_path / "postgres" / "test_eskg_contract_e2e.py"
    security = tmp_path / "security" / "test_spicedb_contract.py"
    for path in (unit, integration, security):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    for name in ("helpers.py", "loader.py", "tasks.py"):
        path = tmp_path / "compute" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    paths = _test_paths(tmp_path)

    assert paths == tuple(
        sorted(
            str(path)
            for path in (
                unit,
                tmp_path / "compute" / "helpers.py",
                tmp_path / "compute" / "loader.py",
                tmp_path / "compute" / "tasks.py",
            )
        )
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
