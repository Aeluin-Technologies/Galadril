"""Build the function-level baseline using Crapper and exact coverage points."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from statistics import fmean
from typing import TypedDict

from check import LANGUAGES, Finding, script_sources
from coverage_records import read_lcov
from crapper.crap import crap_score
from crapper.languages import functions_in_file
from crapper.languages.treesitter import descendants, parser_for


class CoverageStatus(StrEnum):
    """Distinguish lack of execution from lack of instrumentation evidence."""

    MISSING = "missing"
    ZERO = "zero"
    PARTIAL = "partial"
    FULL = "full"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class FunctionMetric:
    """Keep enough identity to compare function metrics across coverage runs."""

    path: str
    line: int
    end_line: int
    domain: str
    component: str
    language: str
    namespace: str
    name: str
    cyclomatic_complexity: int | None
    coverage_percent: float | None
    score: float | None
    status: CoverageStatus
    evidence: str


def _declarations(
    source: str, language: str, stub: bool
) -> list[tuple[int, int, str]]:
    if language == "python" and not stub:
        return []
    kinds = {
        "python": {"function_definition"},
        "rust": {"function_signature_item"},
        "typescript": {"function_signature", "method_signature"},
    }
    data = source.encode()
    tree = parser_for(language).parse(data)
    result: list[tuple[int, int, str]] = []
    for node in descendants(tree.root_node):
        if node.type not in kinds[language]:
            continue
        name = node.child_by_field_name("name")
        if name is not None:
            result.append(
                (
                    node.start_point[0] + 1,
                    node.end_point[0] + 1,
                    data[name.start_byte : name.end_byte].decode(),
                )
            )
    return result


def _status(percent: float | None) -> CoverageStatus:
    if percent is None:
        return CoverageStatus.MISSING
    if percent == 0:
        return CoverageStatus.ZERO
    if percent == 100:
        return CoverageStatus.FULL
    return CoverageStatus.PARTIAL


def _component(path: Path, domain: str) -> str:
    parts = path.parts
    if len(parts) < 3:
        return domain
    if domain == "plugins":
        if len(parts) > 4 and parts[:3] == (
            "plugins",
            "src",
            "galadril_plugins",
        ):
            return parts[3]
        if len(parts) > 3 and parts[1] == "utils":
            return f"utils/{parts[2]}"
    return parts[1]


def measure(
    root: Path, paths: list[str], lcov: str, roots: tuple[Path, ...]
) -> list[FunctionMetric]:
    """Assign scores only to callable code with instrumented points in scope."""

    sources = {
        path: (root / path).read_text(encoding="utf-8") for path in paths
    }
    report = read_lcov(
        lcov,
        {path: source.count("\n") + 1 for path, source in sources.items()},
        roots,
    )
    metrics: list[FunctionMetric] = []
    for path, source in sources.items():
        relative = Path(path)
        stub = relative.suffix == ".pyi"
        language = "python" if stub else LANGUAGES.get(relative.suffix)
        if language is None:
            continue
        display_language = "vue" if relative.suffix == ".vue" else language
        domain = relative.parts[0] if len(relative.parts) > 1 else "repository"
        component = _component(relative, domain)
        points = report.get(path)

        for script in script_sources(path, source):
            for start, end, name in _declarations(script, language, stub):
                metrics.append(
                    FunctionMetric(
                        path,
                        start,
                        end,
                        domain,
                        component,
                        display_language,
                        "",
                        name,
                        None,
                        None,
                        None,
                        CoverageStatus.NOT_APPLICABLE,
                        "declaration-without-runtime-body",
                    )
                )
            if stub:
                continue
            for function in functions_in_file(
                language, script, path, str(root)
            ):
                start, end = function.start_line, function.end_line
                percent = (
                    points.percent(start, end, function.name)
                    if points is not None
                    else None
                )
                metrics.append(
                    FunctionMetric(
                        path,
                        start,
                        end,
                        domain,
                        component,
                        display_language,
                        function.namespace,
                        function.name,
                        function.complexity,
                        percent,
                        crap_score(function.complexity, percent),
                        _status(percent),
                        "bazel-lcov"
                        if percent is not None
                        else "no-points-in-function",
                    )
                )
    return metrics


class ComponentSummary(TypedDict):
    language: str
    domain: str
    component: str
    function_count: int
    measured_count: int
    coverage_status: dict[CoverageStatus, int]
    complexity_total: int
    complexity_maximum: int | None
    coverage_mean_percent: float | None
    crap_mean: float | None
    crap_maximum: float | None
    crap_at_target: int
    crap_needs_review: int
    crap_blocking: int


def summarize(metrics: list[FunctionMetric]) -> list[ComponentSummary]:
    """Group function-weighted metrics without imputing coverage for missing code."""
    groups: defaultdict[tuple[str, str, str], list[FunctionMetric]] = (
        defaultdict(list)
    )
    for metric in metrics:
        groups[metric.language, metric.domain, metric.component].append(metric)

    summaries: list[ComponentSummary] = []
    for (language, domain, component), functions in sorted(groups.items()):
        complexity = [
            row.cyclomatic_complexity
            for row in functions
            if row.cyclomatic_complexity is not None
        ]
        percentages = [
            row.coverage_percent
            for row in functions
            if row.coverage_percent is not None
        ]
        scores = [row.score for row in functions if row.score is not None]
        summaries.append(
            {
                "language": language,
                "domain": domain,
                "component": component,
                "function_count": len(functions),
                "measured_count": len(percentages),
                "coverage_status": dict(
                    Counter(row.status for row in functions)
                ),
                "complexity_total": sum(complexity),
                "complexity_maximum": max(complexity, default=None),
                "coverage_mean_percent": fmean(percentages)
                if percentages
                else None,
                "crap_mean": fmean(scores) if scores else None,
                "crap_maximum": max(scores, default=None),
                "crap_at_target": sum(score < 10 for score in scores),
                "crap_needs_review": sum(10 <= score <= 30 for score in scores),
                "crap_blocking": sum(score > 30 for score in scores),
            }
        )
    return summaries


def findings_for(metrics: list[FunctionMetric]) -> list[Finding]:
    """Report unmeasured code and scores that require review or block delivery."""

    findings: list[Finding] = []
    for metric in metrics:
        if metric.status is CoverageStatus.MISSING:
            rule = "coverage-missing"
            detail = f"{metric.name} CC={metric.cyclomatic_complexity}"
        elif (
            metric.score is not None
            and metric.coverage_percent is not None
            and metric.score >= 10
        ):
            rule = "crap-block" if metric.score > 30 else "crap-target"
            detail = (
                f"{metric.name} CC={metric.cyclomatic_complexity} "
                f"coverage={metric.coverage_percent:.2f}% CRAP={metric.score:.4f}"
            )
        else:
            continue
        findings.append(Finding(metric.path, metric.line, rule, detail))
    return findings


def crap_violations(root: Path, paths: list[str], lcov: str) -> list[Finding]:
    """Evaluate small coverage fixtures without an invocation receipt."""

    return findings_for(measure(root, paths, lcov, (root,)))
