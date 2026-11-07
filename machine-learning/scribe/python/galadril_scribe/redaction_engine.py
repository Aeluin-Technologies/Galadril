"""Destructive redaction preserves unselected glyph positions and rejects unsupported PDFs."""

import base64
import ctypes
import io
import math
from collections import Counter
from contextlib import closing
from dataclasses import dataclass

import pypdfium2 as pdfium
import re2
from pdf_oxide import PdfDocument

from galadril_scribe.redaction_contracts import (
    MAX_DOCUMENT_BYTES,
    MAX_MATCHES,
    MAX_TEXT_CHARS,
    RedactionInput,
    RedactionResult,
    RedactionRule,
)
from galadril_scribe.redaction_pdf import (
    Mask,
    character_text,
    remove_pdf_glyphs,
)

_EMAIL = r"\b[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9.-]*[A-Z0-9])?\.[A-Z]{2,63}\b"
_PHONE = r"(?:\b0[1-9]|\+33[ .-]?[1-9])(?:[ .-]?[0-9]{2}){4}\b"

_WORDS = re2.compile(r"[\p{L}][\p{L}\p{M}'’\-]*")
_NAME_CONTINUATION = re2.compile(r"[\p{L}\p{M}\p{N}_'’\-]")
_TITLE = re2.compile(
    r"(?i)^(Monsieur|Madame|M\.|Mme|Me|Mr\.?|Mrs\.?|Ms\.?|Dr\.?|Pr\.?|Prof\.?)[ \t]+"
)


def _initials(name: str) -> str:
    if len(name) > 512:
        raise ValueError("Name match exceeds limit")
    title = _TITLE.match(name)
    prefix = title.group(1) + " " if title is not None else ""
    words = _WORDS.findall(name[title.end() if title is not None else 0 :])
    if not 1 <= len(words) <= 8:
        raise ValueError("Name match requires review")
    result = prefix + " ".join(word[0].upper() + "." for word in words)
    if len(result) > 64:
        raise ValueError("Public initials exceed limit")
    return result


def _initial_alias(name: str) -> str | None:
    """Only abbreviate a supplied given name, without guessing a surname."""
    title = _TITLE.match(name)
    words = name[title.end() if title is not None else 0 :].split()
    if not 2 <= len(words) <= 8 or not all(
        _WORDS.fullmatch(word) for word in words
    ):
        return None
    gap = r"[ \t\x{00A0}\x{202F}]+"
    return gap.join(
        (re2.escape(words[0][0] + "."), *map(re2.escape, words[1:]))
    )


def _whole_name(text: str, start: int, end: int) -> bool:
    return not (
        _NAME_CONTINUATION.fullmatch(text[max(start - 1, 0) : start])
        or _NAME_CONTINUATION.fullmatch(text[end : end + 1])
    )


@dataclass(frozen=True, slots=True)
class _Span:
    start: int
    end: int
    replacement: str
    whole_name: bool


def _find_spans(text: str, rules: tuple[RedactionRule, ...]) -> list[_Span]:
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError("Text limit")
    spans: list[_Span] = []
    for rule in rules:
        pattern = (
            rule.pattern.get_secret_value() if rule.pattern is not None else ""
        )
        alias = None
        if rule.kind == "literal":
            alias = (
                _initial_alias(pattern)
                if rule.replacement_mode == "initials"
                else None
            )
            pattern = re2.escape(pattern)
            if alias is not None:
                pattern = f"(?:{pattern}|({alias}))"
        elif rule.kind == "email":
            pattern = _EMAIL
        elif rule.kind == "phone":
            pattern = _PHONE
        options = re2.Options()
        options.case_sensitive = rule.case_sensitive
        options.max_mem = 8 * 1024 * 1024
        options.log_errors = False
        compiled = re2.compile(pattern, options=options)
        for match in compiled.finditer(text):
            start, end = match.span()
            whole_name = alias is not None and match.group(1) is not None
            if whole_name and not _whole_name(text, start, end):
                continue
            if start == end or len(spans) >= MAX_MATCHES:
                raise ValueError("Match limit or empty match")
            replacement = (
                _initials(match.group(0))
                if rule.replacement_mode == "initials"
                else rule.replacement
            )
            spans.append(_Span(start, end, replacement, whole_name))
    spans.sort(key=lambda span: (span.start, span.end))
    result: list[_Span] = []
    previous = 0
    last: _Span | None = None
    for span in spans:
        if last is not None and (span.start, span.end, span.replacement) == (
            last.start,
            last.end,
            last.replacement,
        ):
            continue
        if span.start < previous:
            raise ValueError("Overlapping rules require review")
        result.append(span)
        previous = span.end
        last = span
    return result


def apply_rules(text: str, rules: tuple[RedactionRule, ...]) -> tuple[str, int]:
    spans = _find_spans(text, rules)
    parts: list[str] = []
    previous = 0
    for span in spans:
        parts.extend((text[previous : span.start], span.replacement))
        previous = span.end
    parts.append(text[previous:])
    return "".join(parts), len(spans)


@dataclass(frozen=True, slots=True)
class _Glyph:
    char: str
    x: float
    y: float
    width: float
    height: float
    size: float


def _glyph_text(glyphs: list[_Glyph]) -> tuple[str, list[_Glyph | None]]:
    parts: list[str] = []
    mapping: list[_Glyph | None] = []
    previous: _Glyph | None = None
    for glyph in glyphs:
        if (
            previous is not None
            and abs(glyph.y - previous.y) > max(glyph.size, previous.size) * 0.6
        ):
            parts.append("\n")
            mapping.append(None)
        parts.append(glyph.char)
        mapping.extend([glyph] * len(glyph.char))
        previous = glyph
    return "".join(parts), mapping


def _key(glyph: _Glyph) -> tuple[str, int, int]:
    return glyph.char, round(glyph.x * 10), round(glyph.y * 10)


def _glyphs(document: pdfium.PdfDocument, page: int) -> list[_Glyph]:
    result: list[_Glyph] = []
    with (
        closing(document[page]) as current,
        closing(current.get_textpage()) as text,
    ):
        if text.count_chars() > MAX_TEXT_CHARS:
            raise ValueError("PDF text limit")
        for index in range(text.count_chars()):
            char = character_text(text, index)
            if char.isspace():
                previous = result[-1] if result else _Glyph("", 0, 0, 0, 1, 1)
                result.append(
                    _Glyph(
                        char,
                        previous.x + previous.width,
                        previous.y,
                        0,
                        previous.height,
                        previous.size,
                    )
                )
                continue
            x, y = ctypes.c_double(), ctypes.c_double()
            if not char.isprintable() or not pdfium.raw.FPDFText_GetCharOrigin(
                text.raw, index, x, y
            ):
                raise ValueError("Unsupported PDF character mapping")
            left, bottom, right, top = text.get_charbox(index, loose=True)
            size = pdfium.raw.FPDFText_GetFontSize(text.raw, index)
            if (
                not all(
                    math.isfinite(value)
                    for value in (
                        x.value,
                        y.value,
                        left,
                        bottom,
                        right,
                        top,
                        size,
                    )
                )
                or size <= 0
            ):
                raise ValueError("Invalid PDF geometry")
            result.append(
                _Glyph(
                    char,
                    x.value,
                    y.value,
                    max(0, right - x.value),
                    max(0.01, top - bottom),
                    size,
                )
            )
    return result


def _counters(source: bytes) -> list[Counter[tuple[str, int, int]]]:
    with pdfium.PdfDocument(source) as document:
        return [
            Counter(
                _key(glyph)
                for glyph in _glyphs(document, page)
                if glyph.char.strip()
            )
            for page in range(len(document))
        ]


@dataclass(frozen=True, slots=True)
class _Label:
    page: int
    x: float
    y: float
    width: float
    size: float
    text: str


def _edit_pdf(
    source: bytes, labels: list[_Label], remove_annotations: bool
) -> tuple[bytes, bool]:
    removed = False
    with pdfium.PdfDocument(source) as document:
        if pdfium.raw.FPDF_GetFormType(document.raw) != 0:
            raise ValueError("Interactive PDF forms require private review")
        attachments = document.count_attachments()
        if attachments > 64:
            raise ValueError("PDF attachment limit")
        for index in reversed(range(attachments)):
            document.del_attachment(index)
            removed = True
        for number in range(len(document)):
            with closing(document[number]) as page:
                if pdfium.raw.FPDFPage_GetRawThumbnailData(page.raw, None, 0):
                    raise ValueError("PDF thumbnail sanitization unavailable")
                if any(
                    pdfium.raw.FPDFPageObj_CountMarks(obj.raw) != 0
                    for obj in page.get_objects()
                ):
                    raise ValueError("Unsupported PDF marked content")
                # Raster images may carry separate private metadata. They need
                # a dedicated sanitizer before this text-only contract admits them.
                if any(
                    obj.type
                    in {
                        pdfium.raw.FPDF_PAGEOBJ_IMAGE,
                        pdfium.raw.FPDF_PAGEOBJ_FORM,
                    }
                    for obj in page.get_objects()
                ):
                    raise ValueError(
                        "Image and Form XObject sanitization unavailable"
                    )
                if remove_annotations:
                    count = pdfium.raw.FPDFPage_GetAnnotCount(page.raw)
                    if count < 0 or count > MAX_MATCHES:
                        raise ValueError("PDF annotation limit")
                    for index in reversed(range(count)):
                        if not pdfium.raw.FPDFPage_RemoveAnnot(page.raw, index):
                            raise ValueError("PDF annotation removal failed")
                        removed = True
                for label in labels:
                    if label.page != number:
                        continue
                    obj = pdfium.raw.FPDFPageObj_NewTextObj(
                        document.raw, b"Helvetica", label.size
                    )
                    if not obj:
                        raise ValueError("Public label unavailable")
                    inserted = False
                    try:
                        text = label.text.encode("utf-16-le") + b"\0\0"
                        buffer = ctypes.create_string_buffer(text)
                        if not pdfium.raw.FPDFText_SetText(
                            obj,
                            ctypes.cast(
                                buffer, ctypes.POINTER(ctypes.c_ushort)
                            ),
                        ):
                            raise ValueError("Public label unavailable")
                        left, bottom, right, top = (
                            ctypes.c_float() for _ in range(4)
                        )
                        if not pdfium.raw.FPDFPageObj_GetBounds(
                            obj,
                            ctypes.byref(left),
                            ctypes.byref(bottom),
                            ctypes.byref(right),
                            ctypes.byref(top),
                        ):
                            raise ValueError("Public label measurement failed")
                        if right.value - left.value > label.width + 0.1:
                            raise ValueError("Public label does not fit")
                        pdfium.raw.FPDFPageObj_Transform(
                            obj, 1, 0, 0, 1, label.x, label.y
                        )
                        if not pdfium.raw.FPDFPageObj_SetFillColor(
                            obj, 0, 0, 0, 255
                        ):
                            raise ValueError("Public label color unavailable")
                        inserted = True
                        if not pdfium.raw.FPDFPage_InsertObject(page.raw, obj):
                            raise ValueError("Public label insertion failed")
                    finally:
                        if not inserted:
                            pdfium.raw.FPDFPageObj_Destroy(obj)
                page.gen_content()
        output = io.BytesIO()
        document.save(output, flags=pdfium.raw.FPDF_NO_INCREMENTAL)
        return output.getvalue(), removed


def _redact_pdf(
    source: bytes, rules: tuple[RedactionRule, ...]
) -> tuple[bytes, int, int, bool]:
    with pdfium.PdfDocument(source) as original:
        outline_chars = 0
        for index, bookmark in enumerate(original.get_toc()):
            title = bookmark.get_title()
            outline_chars += len(title)
            if index >= MAX_MATCHES or outline_chars > MAX_TEXT_CHARS:
                raise ValueError("PDF outline limit")
            if _find_spans(title, rules):
                raise ValueError("Private PDF outline requires review")
    source, annotations_removed = _edit_pdf(source, [], True)
    document = PdfDocument.from_bytes(source)
    pages = int(document.page_count)
    if not 1 <= pages <= 128 or document.get_layers():
        raise ValueError("Unsupported PDF structure")
    survivors: list[Counter[tuple[str, int, int]]] = []
    removed_glyphs: list[set[tuple[str, int, int]]] = []
    labels: list[_Label] = []
    masks: list[Mask] = []
    secrets: set[tuple[str, bool]] = set()
    count = 0
    characters = 0
    with pdfium.PdfDocument(source) as native:
        for page in range(pages):
            glyphs = _glyphs(native, page)
            text, mapping = _glyph_text(glyphs)
            characters += len(text)
            if not text.strip() or characters > MAX_TEXT_CHARS:
                raise ValueError("Readable text layer required")
            spans = _find_spans(text, rules)
            count += len(spans)
            if count > MAX_MATCHES:
                raise ValueError("PDF match limit")
            selected: set[_Glyph] = set()
            for span in spans:
                secret = text[span.start : span.end]
                if (
                    not secret.strip()
                    or secret.casefold() in span.replacement.casefold()
                ):
                    raise ValueError("Invalid public replacement")
                secrets.add((secret.casefold(), span.whole_name))
                matched = [
                    glyph
                    for glyph in mapping[span.start : span.end]
                    if glyph is not None
                ]
                selected.update(matched)
                lines: dict[float, list[_Glyph]] = {}
                for glyph in matched:
                    lines.setdefault(round(glyph.y, 1), []).append(glyph)
                for line in lines.values():
                    left = min(g.x for g in line)
                    bottom = min(g.y for g in line)
                    right = max(g.x + g.width for g in line)
                    top = max(g.y + g.height for g in line)
                    if span.replacement == "[REDACTED]":
                        height = top - bottom
                        inset = min(0.5, (right - left) * 0.1)
                        masks.append(
                            (
                                page,
                                (
                                    left + inset,
                                    bottom + height * 0.15,
                                    right - inset,
                                    bottom + height * 0.6,
                                ),
                            )
                        )
                if span.replacement != "[REDACTED]":
                    if len(lines) != 1 or not matched:
                        raise ValueError("Multiline label requires review")
                    first = matched[0]
                    labels.append(
                        _Label(
                            page,
                            first.x,
                            first.y,
                            max(g.x + g.width for g in matched) - first.x,
                            first.size,
                            span.replacement,
                        )
                    )
            removed_glyphs.append(
                {_key(glyph) for glyph in selected if glyph.char.strip()}
            )
            survivors.append(
                Counter(
                    _key(glyph)
                    for glyph in glyphs
                    if glyph not in selected and glyph.char.strip()
                )
            )
    output = remove_pdf_glyphs(source, removed_glyphs, masks)
    for page, remaining in enumerate(_counters(output)):
        if any(key in remaining for key in removed_glyphs[page]):
            raise ValueError("Selected PDF glyphs remain")
        if survivors[page] - remaining:
            raise ValueError("Unrelated PDF content changed")
    if labels:
        output, _ = _edit_pdf(output, labels, False)
    cleaned = PdfDocument.from_bytes(output)
    cleaned.sanitize_document()
    output = cleaned.to_bytes(compress=False, garbage_collect=True)
    verifier = PdfDocument.from_bytes(output)
    if int(verifier.page_count) != pages or verifier.xmp_metadata():
        raise ValueError("PDF verification failed")
    final_text = ""
    for page, actual in enumerate(_counters(output)):
        if survivors[page] - actual:
            raise ValueError("Unrelated PDF content changed")
        final_text += verifier.extract_text(page, extract_tables=False) + "\n"
    if any(label.text not in final_text for label in labels):
        raise ValueError("Public initials could not be preserved")
    folded = final_text.casefold()
    for secret, whole_name in secrets:
        for occurrence in re2.finditer(re2.escape(secret), folded):
            if not whole_name or _whole_name(folded, *occurrence.span()):
                raise ValueError("Private text remains")
    with pdfium.PdfDocument(output) as verification:
        if (
            any(verification.get_metadata_dict().values())
            or verification.count_attachments()
            or pdfium.raw.FPDFDoc_GetJavaScriptActionCount(verification.raw)
        ):
            raise ValueError("Private PDF metadata remains")
    return output, count, pages, annotations_removed


def project_document(request: RedactionInput) -> RedactionResult:
    source = base64.b64decode(
        request.content_base64.get_secret_value(), validate=True
    )
    if not source or len(source) > MAX_DOCUMENT_BYTES:
        raise ValueError("Document limit")
    warnings = []
    pages = 0
    if request.mime == "application/pdf":
        if not source.startswith(b"%PDF-"):
            raise ValueError("Invalid PDF")
        output, count, pages, removed = _redact_pdf(source, request.rules)
        if removed:
            warnings.append("annotations_removed")
    else:
        projected, count = apply_rules(source.decode("utf-8"), request.rules)
        output = projected.encode("utf-8")
    if len(output) > MAX_DOCUMENT_BYTES:
        raise ValueError("Output limit")
    if count == 0:
        warnings.append("no_matches")
    return RedactionResult.model_validate(
        {
            "mime": request.mime,
            "content_base64": base64.b64encode(output).decode("ascii"),
            "match_count": count,
            "pages": pages,
            "warnings": warnings,
            "layout_preserved": True,
        }
    )
