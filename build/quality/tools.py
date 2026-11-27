"""Resolve native and Python tools from the Bazel-installed wheels."""

import sys
from importlib.util import find_spec
from pathlib import Path


def tool_command(tool: str) -> list[str]:
    """Resolve Ruff from its wheel install tree before launching it."""

    if tool != "ruff":
        return [sys.executable, "-B", "-m", tool]

    package = find_spec("ruff")
    if package is None or package.origin is None:
        raise ModuleNotFoundError("Pinned Ruff wheel is unavailable")
    installed = Path(package.origin).resolve().parent.parent

    # rules_py links site-packages to wheel install trees but leaves scripts
    # at the wheel's install root. Ruff's RECORD places it in ../../../bin.
    executable = (installed / "../../../bin/ruff").resolve()
    if not executable.is_file():
        raise FileNotFoundError("Pinned Ruff wheel has no executable")
    return [str(executable)]
