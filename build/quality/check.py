"""Check repository boundaries and score functions against Bazel LCOV."""

import re
import tokenize
from ast import Import, ImportFrom, walk
from ast import parse as parse_python
from dataclasses import dataclass
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path

from crapper.languages.treesitter import descendants, parser_for

LANGUAGES = {
    ".py": "python",
    ".rs": "rust",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".vue": "typescript",
}

OUTER_LAYERS = {"adapter", "adapters", "infrastructure", "presentation"}
HOSTS = {"platform", "services", "front"}
PYTHON_SUPPRESSION = re.compile(
    r"^#\s*(?:(?:type|pyright|basedpyright)\s*:\s*ignore\b"
    r"|(?:based)?pyright\s*:)"
)
VUE_SUPPRESSION = re.compile(r"^\s*@vue-(?:ignore|expect-error|skip)\b")
RUST_PATH_SEGMENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")


@dataclass(frozen=True, slots=True)
class Finding:
    """Identify a failing rule at a stable source location."""

    path: str
    line: int
    rule: str
    detail: str


class VueSource(HTMLParser):
    """Preserve SFC line coordinates for script coverage produced with source maps."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.scripts: list[str] = []
        self.suppressions: list[tuple[int, str]] = []
        self._active = False

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self._active = tag == "script"

    def handle_endtag(self, tag: str) -> None:
        self._active = False

    def handle_data(self, data: str) -> None:
        if self._active:
            self.scripts.append("\n" * (self.getpos()[0] - 1) + data)

    def handle_comment(self, data: str) -> None:
        if VUE_SUPPRESSION.match(data):
            self.suppressions.append((self.getpos()[0], data.strip()))


def _parse_vue(source: str) -> VueSource:
    parser = VueSource()
    parser.feed(source)
    parser.close()
    return parser


def script_sources(path: str, source: str) -> list[str]:
    """Extract Vue logic without moving the LCOV line ranges."""

    return _parse_vue(source).scripts if path.endswith(".vue") else [source]


def _python_imports(source: str) -> list[tuple[int, str]]:
    imports: list[tuple[int, str]] = []
    for node in walk(parse_python(source)):
        if isinstance(node, Import):
            imports.extend((node.lineno, alias.name) for alias in node.names)
        if isinstance(node, ImportFrom):
            parent = node.module or ""
            imports.extend(
                (node.lineno, f"{parent}.{alias.name}") for alias in node.names
            )
    return imports


def _typed_imports(language: str, source: str) -> list[tuple[int, str]]:
    data = source.encode()
    tree = parser_for(language).parse(data)
    imports: list[tuple[int, str]] = []

    for node in descendants(tree.root_node):
        if language == "rust":
            if node.type in {
                "use_declaration",
                "scoped_identifier",
                "scoped_type_identifier",
            }:
                text = data[node.start_byte : node.end_byte].decode()
                line = node.start_point[0] + 1
                imports.extend(
                    (line, match.group())
                    for match in RUST_PATH_SEGMENT.finditer(text)
                )
            continue

        target = None
        if node.type in {"import_statement", "export_statement"}:
            target = node.child_by_field_name("source")
        if node.type == "call_expression":
            function = node.child_by_field_name("function")
            args = node.child_by_field_name("arguments")
            if function is not None and args is not None:
                callee = data[function.start_byte : function.end_byte]
                if callee in {b"import", b"require"}:
                    target = args.named_child(0)
        if target is not None and target.type == "string":
            imports.append(
                (
                    node.start_point[0] + 1,
                    data[target.start_byte + 1 : target.end_byte - 1].decode(),
                )
            )
    return imports


def architecture_violations(path: str, source: str) -> list[Finding]:
    """Enforce inward imports, plugin isolation and Python typing directives."""

    source_path = Path(path)
    parts = source_path.parts[:-1]
    layers = {*parts, source_path.stem}
    forbidden: set[str] = set()
    if {"domain", "application"}.intersection(layers):
        forbidden.update(OUTER_LAYERS)
    if "domain" in layers:
        forbidden.add("application")
    if path.startswith("front/ui/src/"):
        forbidden.add("dashboard")

    findings: set[Finding] = set()
    imports: list[tuple[int, str]]
    if path.endswith((".py", ".pyi")):
        imports = _python_imports(source)
        findings.update(
            Finding(path, token.start[0], "type-suppression", token.string)
            for token in tokenize.generate_tokens(StringIO(source).readline)
            if token.type == tokenize.COMMENT
            and PYTHON_SUPPRESSION.match(token.string)
        )
    elif path.endswith(".vue"):
        vue = _parse_vue(source)
        findings.update(
            Finding(path, line, "type-suppression", detail)
            for line, detail in vue.suppressions
        )
        imports = []
        if forbidden:
            for script in vue.scripts:
                imports.extend(_typed_imports("typescript", script))
    elif forbidden:
        grammar = "rust" if path.endswith(".rs") else "typescript"
        imports = _typed_imports(grammar, source)
    else:
        return []

    for line, name in imports:
        segments = re.split(r"[./:]+", name)
        plugin = (
            bool(parts)
            and parts[0] in HOSTS
            and "galadril_plugins" in segments
            and bool({"law", "utils"}.intersection(segments))
        )
        if forbidden.intersection(segments) or plugin:
            findings.add(Finding(path, line, "dependency-direction", name))

    return sorted(
        findings,
        key=lambda finding: (finding.line, finding.rule, finding.detail),
    )
