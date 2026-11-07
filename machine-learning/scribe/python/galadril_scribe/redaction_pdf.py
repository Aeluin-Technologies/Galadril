"""Retain original glyph origins when a PDF's text advances cannot be preserved."""

from __future__ import annotations

import ctypes
import io
import math
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass

import pypdfium2 as pdfium
from pdf_oxide import PdfDocument

from galadril_scribe.redaction_contracts import MAX_TEXT_CHARS

GlyphKey = tuple[str, int, int]
Mask = tuple[int, tuple[float, float, float, float]]


@dataclass(frozen=True, slots=True)
class _Character:
    text: str
    x: float
    y: float
    removed: bool
    hyphen_box: tuple[float, float, float, float] | None


def character_text(text: pdfium.PdfTextPage, index: int) -> str:
    code = pdfium.raw.FPDFText_GetUnicode(text.raw, index)
    if code == 2 and pdfium.raw.FPDFText_IsHyphen(text.raw, index) == 1:
        return "-"
    if pdfium.raw.FPDFText_HasUnicodeMapError(text.raw, index) == 1:
        raise ValueError("Unsupported PDF character mapping")
    return chr(code)


def _characters(
    page: pdfium.PdfPage, selected: set[GlyphKey]
) -> tuple[dict[int, list[_Character]], set[int]]:
    records: dict[int, list[_Character]] = {}
    affected: set[int] = set()
    unmatched = set(selected)
    with closing(page.get_textpage()) as text:
        count = text.count_chars()
        if count > MAX_TEXT_CHARS:
            raise ValueError("PDF character limit")
        for index in range(count):
            if pdfium.raw.FPDFText_IsGenerated(text.raw, index):
                continue
            obj = pdfium.raw.FPDFText_GetTextObject(text.raw, index)
            object_id = ctypes.cast(obj, ctypes.c_void_p).value
            if object_id is None:
                continue
            x, y = ctypes.c_double(), ctypes.c_double()
            if not pdfium.raw.FPDFText_GetCharOrigin(text.raw, index, x, y):
                raise ValueError("PDF character origin unavailable")
            if not math.isfinite(x.value) or not math.isfinite(y.value):
                raise ValueError("Invalid PDF character origin")
            char = character_text(text, index)
            key = (char, round(x.value * 10), round(y.value * 10))
            match = key if key in selected else None
            if match is not None:
                affected.add(object_id)
                unmatched.discard(match)
            records.setdefault(object_id, []).append(
                _Character(
                    char,
                    x.value,
                    y.value,
                    match is not None,
                    text.get_charbox(index) if char == "-" else None,
                )
            )
    if unmatched:
        raise ValueError("PDF character mapping requires review")
    return records, affected


def _insert_character(
    page: pdfium.PdfPage, original: pdfium.PdfTextObj, char: _Character
) -> None:
    if not char.text.isprintable():
        raise ValueError("Unsupported PDF character")
    if pdfium.raw.FPDFTextObj_GetTextRenderMode(original.raw) != 0:
        raise ValueError("Unsupported PDF text rendering mode")
    matrix = original.get_matrix()
    if not all(math.isfinite(value) for value in matrix.get()):
        raise ValueError("Invalid PDF text matrix")
    font = original.get_font()
    raw = pdfium.raw.FPDFPageObj_CreateTextObj(
        page.pdf.raw, font.raw, original.get_font_size()
    )
    if not raw:
        raise ValueError("PDF text reconstruction unavailable")
    transferred = False
    try:
        encoded = ctypes.create_string_buffer(
            char.text.encode("utf-16-le") + b"\0\0"
        )
        if not pdfium.raw.FPDFText_SetText(
            raw, ctypes.cast(encoded, ctypes.POINTER(ctypes.c_ushort))
        ):
            raise ValueError("PDF character reconstruction unavailable")
        colors = tuple(ctypes.c_uint() for _ in range(4))
        if not pdfium.raw.FPDFPageObj_GetFillColor(
            original.raw, *(ctypes.byref(color) for color in colors)
        ) or not pdfium.raw.FPDFPageObj_SetFillColor(
            raw, *(color.value for color in colors)
        ):
            raise ValueError("PDF text color unavailable")
        position = pdfium.raw.FS_MATRIX(
            matrix.a, matrix.b, matrix.c, matrix.d, char.x, char.y
        )
        if not pdfium.raw.FPDFPageObj_SetMatrix(raw, ctypes.byref(position)):
            raise ValueError("PDF character matrix unavailable")
        if char.hyphen_box is not None:
            bounds = tuple(ctypes.c_float() for _ in range(4))

            def same_shape() -> bool:
                return bool(
                    pdfium.raw.FPDFPageObj_GetBounds(
                        raw, *(ctypes.byref(value) for value in bounds)
                    )
                ) and all(
                    abs(value.value - expected) < 0.01
                    for value, expected in zip(
                        bounds, char.hyphen_box, strict=True
                    )
                )

            if not same_shape():
                # Subset fonts can retain a hyphen glyph without a reverse Unicode map.
                codes = (ctypes.c_uint32 * 1)(45)
                if (
                    not pdfium.raw.FPDFText_SetCharcodes(raw, codes, 1)
                    or not same_shape()
                ):
                    raise ValueError(
                        "PDF hyphen reconstruction requires review"
                    )
        # InsertObject consumes the handle even on failure in the pinned PDFium ABI.
        transferred = True
        if not pdfium.raw.FPDFPage_InsertObject(page.raw, raw):
            raise ValueError("PDF character insertion failed")
    finally:
        if not transferred:
            pdfium.raw.FPDFPageObj_Destroy(raw)


def remove_pdf_glyphs(
    source: bytes, selected: Sequence[set[GlyphKey]], masks: Sequence[Mask]
) -> bytes:
    """Only affected text objects are split; no paragraph or page is laid out again."""
    created = 0
    with pdfium.PdfDocument(source) as document:
        if len(document) != len(selected) or pdfium.raw.FPDFCatalog_IsTagged(
            document.raw
        ):
            raise ValueError("Unsupported PDF structure")
        for number, keys in enumerate(selected):
            if not keys:
                continue
            with closing(document[number]) as page:
                records, affected = _characters(page, keys)
                originals = list(page.get_objects())
                for original in originals:
                    object_id = ctypes.cast(original.raw, ctypes.c_void_p).value
                    if object_id not in affected:
                        continue
                    if (
                        not isinstance(original, pdfium.PdfTextObj)
                        or original.level
                    ):
                        raise ValueError("Unsupported nested PDF text")
                    for char in records.get(object_id, ()):
                        if char.removed or char.text.isspace():
                            continue
                        created += 1
                        if created > 32768:
                            raise ValueError("PDF reconstruction limit")
                        _insert_character(page, original, char)
                    page.remove_obj(original)
                    original.close()
                for mask_page, (left, bottom, right, top) in masks:
                    if mask_page != number:
                        continue
                    raw = pdfium.raw.FPDFPageObj_CreateNewRect(
                        left, bottom, right - left, top - bottom
                    )
                    if not raw:
                        raise ValueError("PDF mask unavailable")
                    transferred = False
                    try:
                        if not pdfium.raw.FPDFPageObj_SetFillColor(
                            raw, 0, 0, 0, 255
                        ) or not pdfium.raw.FPDFPath_SetDrawMode(
                            raw, pdfium.raw.FPDF_FILLMODE_WINDING, 0
                        ):
                            raise ValueError("PDF mask unavailable")
                        transferred = True
                        if not pdfium.raw.FPDFPage_InsertObject(page.raw, raw):
                            raise ValueError("PDF mask insertion failed")
                    finally:
                        if not transferred:
                            pdfium.raw.FPDFPageObj_Destroy(raw)
                page.gen_content()
        output = io.BytesIO()
        document.save(output, flags=pdfium.raw.FPDF_NO_INCREMENTAL)
    cleaned = PdfDocument.from_bytes(output.getvalue())
    return cleaned.to_bytes(compress=False, garbage_collect=True)
