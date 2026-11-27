"""Map Bazel LCOV points to exact repository sources."""

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

type Branch = tuple[int, str, str]


@dataclass(slots=True)
class Coverage:
    """Retain instrumented identities even when every execution count is zero."""

    lines: dict[int, bool] = field(default_factory=dict)
    branches: dict[Branch, bool] = field(default_factory=dict)
    functions: dict[tuple[int, str], bool] = field(default_factory=dict)

    def percent(
        self, start: int, end: int, function: str | None = None
    ) -> float | None:
        """Use branch points when available and distinguish no executable points."""

        if (
            function is not None
            and self.functions.get((start, function)) is False
        ):
            return 0.0

        hits = [
            hit
            for (line, _, _), hit in self.branches.items()
            if start <= line <= end
        ]
        if not hits:
            hits = [
                self.lines[line]
                for line in range(start, end + 1)
                if line in self.lines
            ]
        return 100 * sum(hits) / len(hits) if hits else None


def _source(
    raw: str, lengths: dict[str, int], roots: tuple[Path, ...]
) -> str | None:
    if raw.startswith("file:"):
        url = urlsplit(raw)
        if url.netloc or url.query or url.fragment:
            return None
        raw = unquote(url.path)

    path = PurePosixPath(raw)
    if ".." in path.parts:
        return None
    if not path.is_absolute():
        name = path.as_posix()
        return name if name in lengths else None

    for root in roots:
        if path.is_relative_to(root.as_posix()):
            name = path.relative_to(root.as_posix()).as_posix()
            if name in lengths:
                return name
    # Bazel's runfiles and execroot use stable workspace markers on both hosts.
    for index, part in enumerate(path.parts[1:], 1):
        parent = path.parts[index - 1]
        if part == "_main" and (
            parent == "execroot" or parent.endswith(".runfiles")
        ):
            name = PurePosixPath(*path.parts[index + 1 :]).as_posix()
            if name in lengths:
                return name
    return None


def _line_number(value: str, maximum: int, path: str) -> int:
    line = int(value)
    if not 1 <= line <= maximum:
        raise ValueError(
            f"{path}: LCOV line {line} outside source (1–{maximum})"
        )
    return line


def _hit(value: str) -> bool:
    count = int(value)
    if count < 0:
        raise ValueError("LCOV execution count cannot be negative")
    return count > 0


def read_lcov(
    text: str, lengths: dict[str, int], roots: tuple[Path, ...]
) -> dict[str, Coverage]:
    """Read combined Bazel points without last-record wins or suffix matching."""

    records: dict[str, Coverage] = {}
    in_record = False
    name = None
    declarations: dict[str, set[int]] = {}
    hits_by_function: dict[str, bool] = {}
    for row in text.splitlines():
        if row.startswith("SF:"):
            if in_record:
                raise ValueError("LCOV record missing end_of_record")
            in_record = True
            name = _source(row[3:], lengths, roots)
            continue
        if row == "end_of_record":
            if not in_record:
                raise ValueError("LCOV has end_of_record without SF")
            if name is not None and hits_by_function:
                functions = records.setdefault(name, Coverage()).functions
                for function, hit in hits_by_function.items():
                    lines = declarations.get(function, set())
                    if len(lines) == 1:
                        [line] = lines
                        key = (line, function)
                        functions[key] = functions.get(key, False) or hit
            declarations.clear()
            hits_by_function.clear()
            in_record = False
            name = None
            continue
        if name is None:
            continue

        maximum = lengths[name]
        if row.startswith("DA:"):
            number, count, *_ = row[3:].split(",")
            line = _line_number(number, maximum, name)
            covered = _hit(count)
            lines = records.setdefault(name, Coverage()).lines
            lines[line] = lines.get(line, False) or covered
        elif name.endswith(".vue") and row.startswith("FN:"):
            number, function = row[3:].split(",", 1)
            line = _line_number(number, maximum, name)
            declarations.setdefault(function, set()).add(line)
        elif name.endswith(".vue") and row.startswith("FNDA:"):
            count, function = row[5:].split(",", 1)
            covered = _hit(count)
            hits_by_function[function] = (
                hits_by_function.get(function, False) or covered
            )
        elif row.startswith("BRDA:"):
            number, block, branch, count = row[5:].split(",")
            # coverage.py exports module-entry arcs at line zero, outside Python source.
            if number == "0" and name.endswith(".py"):
                continue
            line = _line_number(number, maximum, name)
            covered = count != "-" and _hit(count)
            branches = records.setdefault(name, Coverage()).branches
            point = (line, block, branch)
            branches[point] = branches.get(point, False) or covered

    if in_record:
        raise ValueError("LCOV record missing end_of_record")
    return records
