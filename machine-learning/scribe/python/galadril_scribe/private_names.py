"""Detect private spans locally and preserve only their initials in model input."""

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import re2

from galadril_scribe.redaction_names import (
    EMAIL_PATTERN,
    PHONE_PATTERN,
    initials,
)

_NAME = r"[A-ZÀ-ÖØ-ÞŒ][^\W\d_]+(?:['’\-‑][^\W\d_]+)*"
_TITLE = re.compile(
    rf"(?<!\w)(?:M\.|Mme\.?|Me\.?|Monsieur|Madame)[ \t\u00a0]+"
    rf"(?P<name>{_NAME}(?:[ \t\u00a0]+(?:de|du|des|la|le|{_NAME})){{0,4}})"
)
_INITIAL = re.compile(
    rf"(?<![\w.])(?P<name>(?:[A-ZÀ-ÖØ-ÞŒ]\.[ \t\u00a0\u202f-]*){{1,3}}{_NAME})(?![\w'’\-‑])"
)
_CONTACT = re2.compile(
    rf"(?i)(?:{EMAIL_PATTERN})|(?:{PHONE_PATTERN})|"
    r"\b[0-9]{1,4}[ ,\t]+(?:rue|avenue|boulevard|chemin|allée|impasse|place)\b[^\r\n;]{3,90}",
)
_WINDOW = 384
_OVERLAP = 96
NameDetector = Callable[[str], list[tuple[int, int]]]


@dataclass(frozen=True, slots=True)
class _Span:
    start: int
    end: int
    mask: bool = False


def _windows(text: str) -> Iterator[tuple[int, str]]:
    start = 0
    while start < len(text):
        end = min(start + _WINDOW, len(text))
        if end < len(text):
            edge = max(
                text.rfind(" ", end - 32, end), text.rfind("\n", end - 32, end)
            )
            if edge >= 0:
                end = edge + 1
        yield start, text[start:end]
        if end == len(text):
            break
        overlap = end - _OVERLAP
        edge = max(
            text.rfind(" ", overlap - 32, overlap),
            text.rfind("\n", overlap - 32, overlap),
        )
        start = edge + 1 if edge >= 0 else overlap


def pseudonymize(text: str, detect: NameDetector) -> str:
    spans: list[_Span] = []
    for start, window in _windows(text):
        for a, b in detect(window):
            if not 0 <= a < b <= len(window):
                raise ValueError("Invalid private detector offsets")
            spans.append(_Span(start + a, start + b))
    spans.extend(
        _Span(*match.span("name"))
        for rule in (_TITLE, _INITIAL)
        for match in rule.finditer(text)
    )
    spans.extend(
        _Span(*match.span(), mask=True) for match in _CONTACT.finditer(text)
    )
    merged: list[_Span] = []
    for span in sorted(spans, key=lambda item: (item.start, item.end)):
        if merged and span.start < merged[-1].end:
            previous = merged.pop()
            span = _Span(
                previous.start,
                max(span.end, previous.end),
                span.mask or previous.mask,
            )
        merged.append(span)
    output: list[str] = []
    cursor = 0
    for span in merged:
        output.append(text[cursor : span.start])
        replacement = "[REDACTED]"
        if not span.mask:
            try:
                replacement = initials(text[span.start : span.end])
            except ValueError:
                replacement = "[PERSON]"
        output.append(replacement)
        cursor = span.end
    output.append(text[cursor:])
    return "".join(output)
