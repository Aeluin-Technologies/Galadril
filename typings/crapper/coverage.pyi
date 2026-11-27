"""Type contract for the pinned Crapper LCOV readers."""

class FileCoverage(dict[int, tuple[int, int]]):
    branches: dict[int, tuple[int, int]]

def parse_lcov(text: str) -> dict[str, FileCoverage]: ...
def branch_percent(
    record: FileCoverage, start: int, end: int
) -> float | None: ...
def percent_for_range(
    lines: FileCoverage, start: int, end: int
) -> float | None: ...
