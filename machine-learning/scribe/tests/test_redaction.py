"""Private source bytes stay in bounded native processing, outside model context."""

import asyncio
import base64
from collections.abc import AsyncIterator, Mapping, Sequence

import httpx
import pypdfium2 as pdfium
import pytest
from fastapi import FastAPI
from galadril_scribe.redaction import RedactionFailure, RedactionRuntime
from galadril_scribe.redaction_contracts import RedactionInput, RedactionRule
from galadril_scribe.redaction_engine import project_document
from pdf_oxide import Pdf, PdfDocument


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def request(
    content: bytes, mime: str, rules: Sequence[Mapping[str, str | bool]]
) -> RedactionInput:
    return RedactionInput.model_validate(
        {
            "mime": mime,
            "content_base64": base64.b64encode(content).decode(),
            "rules": rules,
        }
    )


@pytest.mark.anyio
async def test_text_redaction_uses_literal_replacements_and_preserves_legal_dates() -> (
    None
):
    runtime = RedactionRuntime()
    source = "Jean-Daniel Durand écrit à alice@example.org, au 06 12 34 56 78. CE, 21 septembre 2015, n° 369808."
    result = await runtime.run(
        request(
            source.encode(),
            "text/plain",
            [
                {
                    "kind": "literal",
                    "pattern": "Jean-Daniel Durand",
                    "replacement": "J. D.",
                },
                {"kind": "email"},
                {"kind": "phone"},
            ],
        )
    )
    text = base64.b64decode(result.content_base64).decode()
    assert "Durand" not in text and "alice@example.org" not in text
    assert "06 12 34 56 78" not in text
    assert "J. D." in text and "21 septembre 2015, n° 369808" in text
    assert result.match_count == 3
    assert result.requires_review and result.coverage == "provided_rules_only"
    assert runtime.active == 0


@pytest.mark.anyio
async def test_custom_regex_is_non_backtracking_and_unsupported_syntax_is_refused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = RedactionRuntime()
    for pattern in [r"(a)\1", r"(?<=PRIVATE)NAME", "("]:
        with pytest.raises(RedactionFailure):
            await runtime.run(
                request(
                    b"PRIVATE_NAME",
                    "text/plain",
                    [{"kind": "regex", "pattern": pattern}],
                )
            )
    result = await runtime.run(
        request(
            ("a" * 60000 + "!").encode(),
            "text/plain",
            [{"kind": "regex", "pattern": "^(a+)+$"}],
        )
    )
    assert result.match_count == 0
    assert "PRIVATE_NAME" not in capsys.readouterr().err


@pytest.mark.anyio
@pytest.mark.parametrize("isolated", [False, True])
async def test_pdf_redaction_preserves_layout_and_removes_original_metadata(
    isolated: bool,
) -> None:
    original = Pdf.from_text(
        "Monsieur Durand soutient que l'étudiant a répondu. alice@example.org"
    ).to_bytes()
    edited = PdfDocument.from_bytes(original)
    edited.set_author("PRIVATE_METADATA")
    edited.set_title("PRIVATE_TITLE")
    source = edited.to_bytes()
    original_document = PdfDocument.from_bytes(source)
    original_chars = original_document.extract_chars(0)
    position = "".join(char.char for char in original_chars).index("soutient")
    expected = tuple(
        tuple(char.bbox) for char in original_chars[position : position + 8]
    )
    payload = request(
        source,
        "application/pdf",
        [
            {
                "kind": "literal",
                "pattern": "Monsieur Durand",
                "replacement": "Monsieur D.",
            },
            {"kind": "email"},
        ],
    )
    result = (
        await RedactionRuntime().run(payload)
        if isolated
        else project_document(payload)
    )
    output = base64.b64decode(result.content_base64)
    document = PdfDocument.from_bytes(output)
    text = "\n".join(
        document.extract_text(i) for i in range(int(document.page_count))
    )
    assert "Durand" not in text and "alice@example.org" not in text
    assert "Monsieur D." in text
    with pdfium.PdfDocument(output) as native:
        page = native[0]
        textpage = page.get_textpage()
        try:
            assert "l'étudiant a répondu" in textpage.get_text_range()
        finally:
            textpage.close()
            page.close()
    assert b"PRIVATE_METADATA" not in output and b"PRIVATE_TITLE" not in output
    assert result.layout_preserved
    assert int(document.page_count) == int(original_document.page_count)
    retained_chars = document.extract_chars(0)
    retained = "".join(char.char for char in retained_chars).index("soutient")
    actual = tuple(
        tuple(char.bbox) for char in retained_chars[retained : retained + 8]
    )
    for before, after in zip(expected, actual, strict=True):
        assert after == pytest.approx(before, abs=0.1)
    assert source != output


@pytest.mark.anyio
async def test_malformed_pdf_returns_no_original_or_partial_document(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = RedactionRuntime()
    with pytest.raises(RedactionFailure):
        await runtime.run(
            request(
                b"%PDF-1.7 PRIVATE_SENTINEL invalid",
                "application/pdf",
                [{"kind": "email"}],
            )
        )
    assert runtime.active == 0
    assert "PRIVATE_SENTINEL" not in capsys.readouterr().err


def test_rules_reject_code_callbacks_and_empty_patterns() -> None:
    for value in [
        {"kind": "python", "pattern": "print('unsafe')"},
        {"kind": "regex", "pattern": ""},
        {"kind": "email", "pattern": ".*"},
    ]:
        with pytest.raises(ValueError):
            RedactionRule.model_validate(value)


@pytest.mark.anyio
async def test_native_api_does_not_invoke_models_or_echo_private_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from galadril_scribe import redaction_api

    app = FastAPI()
    redaction_api.install_redaction(app, RedactionRuntime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://scribe"
    ) as client:
        response = await client.post(
            "/documents/redact",
            json={
                "mime": "text/plain",
                "content_base64": base64.b64encode(
                    b"alice@example.org"
                ).decode(),
                "rules": [{"kind": "email"}],
            },
        )
        assert response.status_code == 200
        assert (
            base64.b64decode(response.json()["content_base64"]) == b"[REDACTED]"
        )
        assert response.json()["requires_review"]
        rejected = await client.post(
            "/documents/redact",
            json={
                "mime": "application/pdf",
                "content_base64": base64.b64encode(
                    b"PRIVATE_INVALID_PDF"
                ).decode(),
                "rules": [{"kind": "email"}],
            },
        )
        assert rejected.status_code == 422
        assert "PRIVATE_INVALID_PDF" not in rejected.text
        invalid = await client.post(
            "/documents/redact", json={"mime": "PRIVATE_INVALID_KIND"}
        )
        assert invalid.status_code == 422
        assert "PRIVATE_INVALID_KIND" not in invalid.text
        monkeypatch.setattr(redaction_api, "MAX_WIRE_BYTES", 32)
        assert (
            await client.post("/documents/redact", content=b"x" * 33)
        ).status_code == 413


@pytest.mark.anyio
async def test_redaction_capacity_and_cancellation_release_worker_slots() -> (
    None
):
    runtime = RedactionRuntime()
    runtime.active = 2
    payload = request(b"alice@example.org", "text/plain", [{"kind": "email"}])
    with pytest.raises(RedactionFailure):
        await runtime.run(payload)
    assert runtime.active == 2
    runtime.active = 0
    task = asyncio.create_task(runtime.run(payload))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.active == 0


@pytest.mark.parametrize(
    "rules",
    [
        [{"kind": "regex", "pattern": "^"}],
        [
            {"kind": "literal", "pattern": "alice"},
            {"kind": "literal", "pattern": "alice@example.org"},
        ],
    ],
)
def test_zero_width_and_conflicting_rules_are_refused(
    rules: list[dict[str, str]],
) -> None:
    with pytest.raises(ValueError):
        project_document(request(b"alice@example.org", "text/plain", rules))


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("Jean-Daniel Durand", "J. D."),
        ("Monsieur Durand", "Monsieur D."),
        ("Mme Marie Dupont", "Mme M. D."),
    ],
)
def test_initials_are_generated_locally_without_regex_code(
    source: str, expected: str
) -> None:
    result = project_document(
        request(
            source.encode(),
            "text/plain",
            [
                {
                    "kind": "literal",
                    "pattern": source,
                    "replacement_mode": "initials",
                }
            ],
        )
    )
    assert base64.b64decode(result.content_base64).decode() == expected


@pytest.mark.parametrize("mime", ["text/plain", "application/pdf"])
def test_full_name_rule_also_replaces_an_initial_followed_by_the_surname(
    mime: str,
) -> None:
    source = (
        "R. Lenoir intervient. P. Lenoir répond. R. Lenoir-Dupont confirme."
    )
    original = (
        source.encode()
        if mime == "text/plain"
        else Pdf.from_text(source).to_bytes()
    )
    result = project_document(
        request(
            original,
            mime,
            [
                {
                    "kind": "literal",
                    "pattern": "Rémi Lenoir",
                    "replacement_mode": "initials",
                }
            ],
        )
    )
    output = base64.b64decode(result.content_base64)
    if mime == "application/pdf":
        with pdfium.PdfDocument(output) as document:
            text = document[0].get_textpage().get_text_range()
    else:
        text = output.decode()
    assert result.match_count == 1
    assert "R. Lenoir intervient" not in text
    assert "R. Lenoir-Dupont" in text
    assert "R. L." in text
    assert "P. Lenoir" in text


def test_bounded_unicode_name_patterns_support_compound_names() -> None:
    pattern = r"\b(?:Monsieur|Madame|Jean|Marie|Pierre|René|Jacques|François|Michel|André)[ \t]+[\p{L}][\p{L}\p{M}'’\-]{1,40}(?:[ \t]+[A-ZÀ-ÖØ-Ý][\p{L}\p{M}'’\-]{1,40})?\b"
    result = project_document(
        request(
            b"Monsieur Durand",
            "text/plain",
            [
                {
                    "kind": "regex",
                    "pattern": pattern,
                    "replacement_mode": "initials",
                    "case_sensitive": True,
                }
            ],
        )
    )
    assert base64.b64decode(result.content_base64).decode() == "Monsieur D."


def pdf_fixture(
    content: bytes, custom_widths: bool = False, private_outline: bool = False
) -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        (
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /FirstChar 32 /LastChar 126 /Widths ["
            + b"600 " * 95
            + b"] >>"
            if custom_widths
            else b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
        ),
        b"<< /Length "
        + str(len(content)).encode()
        + b" >>\nstream\n"
        + content
        + b"\nendstream",
    ]
    if private_outline:
        objects[0] = b"<< /Type /Catalog /Pages 2 0 R /Outlines 6 0 R >>"
        objects.extend(
            [
                b"<< /Type /Outlines /First 7 0 R /Last 7 0 R /Count 1 >>",
                b"<< /Title (Durand) /Parent 6 0 R /Dest [3 0 R /Fit] >>",
            ]
        )
    body = bytearray(b"%PDF-1.7\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(body))
        body.extend(str(index).encode() + b" 0 obj\n" + obj + b"\nendobj\n")
    xref = len(body)
    body.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        body.extend(f"{offset:010d} 00000 n \n".encode())
    body.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(body)


def test_pdf_redaction_refuses_private_matches_in_outlines() -> None:
    source = pdf_fixture(
        b"BT /F1 12 Tf 72 705 Td (Durand public text) Tj ET",
        private_outline=True,
    )
    with pytest.raises(ValueError, match="Private PDF outline requires review"):
        project_document(
            request(
                source,
                "application/pdf",
                [{"kind": "literal", "pattern": "Durand"}],
            )
        )


def test_pdf_redaction_refuses_unhandled_marked_content() -> None:
    source = pdf_fixture(
        b"/Span << /ActualText (PRIVATE_SENTINEL) >> BDC "
        b"BT /F1 12 Tf 72 705 Td (Durand public text) Tj ET EMC"
    )
    with pytest.raises(ValueError, match="Unsupported PDF marked content"):
        project_document(
            request(
                source,
                "application/pdf",
                [{"kind": "literal", "pattern": "Durand"}],
            )
        )


def test_pdf_redaction_refuses_unhandled_page_thumbnails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = pdf_fixture(b"BT /F1 12 Tf 72 705 Td (Durand public text) Tj ET")
    monkeypatch.setattr(
        pdfium.raw, "FPDFPage_GetRawThumbnailData", lambda *_: 32
    )
    with pytest.raises(
        ValueError, match="PDF thumbnail sanitization unavailable"
    ):
        project_document(
            request(
                source,
                "application/pdf",
                [{"kind": "literal", "pattern": "Durand"}],
            )
        )


def test_pdf_redaction_keeps_text_on_a_tightly_spaced_adjacent_line() -> None:
    result = project_document(
        request(
            pdf_fixture(
                b"BT /F1 12 Tf 72 705 Td (Reference text must remain intact.) Tj 0 -12 Td (Monsieur Durand soutient sa demande.) Tj ET"
            ),
            "application/pdf",
            [
                {
                    "kind": "literal",
                    "pattern": "Monsieur Durand",
                    "replacement_mode": "initials",
                }
            ],
        )
    )
    document = PdfDocument.from_bytes(base64.b64decode(result.content_base64))
    text = document.extract_text(0)
    assert "Reference text must remain intact." in text
    assert "Monsieur D." in text and "Durand" not in text


def test_pdf_redaction_keeps_custom_font_advances() -> None:
    source = pdf_fixture(
        b"BT /F1 12 Tf 72 705 Td (Monsieur Durand soutient sa demande.) Tj ET",
        custom_widths=True,
    )
    result = project_document(
        request(
            source,
            "application/pdf",
            [
                {
                    "kind": "literal",
                    "pattern": "Monsieur Durand",
                    "replacement_mode": "initials",
                }
            ],
        )
    )
    document = PdfDocument.from_bytes(base64.b64decode(result.content_base64))
    assert "soutient sa demande" in document.extract_text(0)


@pytest.mark.parametrize(
    "state", [b"1.25 Tc", b"83 Tz", b"0.8 0 0 1 72 705 Tm"]
)
def test_pdf_redaction_preserves_text_state_advances(state: bytes) -> None:
    source = pdf_fixture(
        b"BT /F1 12 Tf 72 705 Td "
        + state
        + b" [(Monsieur Du) -20 (rand) -40 ( soutient sa demande.)] TJ ET"
    )
    result = project_document(
        request(
            source,
            "application/pdf",
            [
                {
                    "kind": "literal",
                    "pattern": "Monsieur Durand",
                    "replacement_mode": "initials",
                }
            ],
        )
    )
    assert result.match_count == 1
    document = PdfDocument.from_bytes(base64.b64decode(result.content_base64))
    assert "Durand" not in document.extract_text(0)


@pytest.mark.anyio
async def test_native_api_limits_inflight_bodies_before_parsing_private_data() -> (
    None
):
    from galadril_scribe.redaction_api import install_redaction

    app = FastAPI()
    install_redaction(app, RedactionRuntime())
    entered = asyncio.Semaphore(0)
    release = asyncio.Event()

    async def body() -> AsyncIterator[bytes]:
        entered.release()
        await release.wait()
        yield b"{}"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://scribe"
    ) as client:
        pending = [
            asyncio.create_task(
                client.post("/documents/redact", content=body())
            )
            for _ in range(2)
        ]
        try:
            for _ in pending:
                await asyncio.wait_for(entered.acquire(), timeout=1)
            response = await client.post(
                "/documents/redact",
                json={
                    "mime": "text/plain",
                    "content_base64": "eA==",
                    "rules": [{"kind": "email"}],
                },
            )
            assert response.status_code == 429
        finally:
            release.set()
            await asyncio.gather(*pending)


def test_character_surgery_keeps_unselected_glyph_origins_and_removes_originals() -> (
    None
):
    from collections import Counter

    from galadril_scribe.redaction_engine import _glyphs, _key
    from galadril_scribe.redaction_pdf import remove_pdf_glyphs

    source = pdf_fixture(
        b"BT /F1 12 Tf 72 705 Td 1.25 Tc 83 Tz [(Monsieur Du) -20 (rand) -40 ( soutient sa demande.)] TJ ET"
    )
    with pdfium.PdfDocument(source) as document:
        glyphs = _glyphs(document, 0)
    text = "".join(glyph.char for glyph in glyphs)
    start = text.index("Durand")
    selected = {_key(glyph) for glyph in glyphs[start : start + 6]}
    output = remove_pdf_glyphs(source, [selected], [])
    with pdfium.PdfDocument(output) as document:
        remaining = _glyphs(document, 0)
    assert not any(_key(glyph) in selected for glyph in remaining)
    assert Counter(
        _key(glyph) for glyph in remaining if glyph.char.strip()
    ) == Counter(
        _key(glyph)
        for glyph in glyphs
        if glyph.char.strip() and _key(glyph) not in selected
    )


def test_pdfium_discretionary_hyphens_are_not_treated_as_unknown_characters() -> (
    None
):
    source = pdf_fixture(
        b"BT /F1 12 Tf 72 705 Td (Monsieur Durand cite une proce-) Tj 0 -14 Td (dure distincte.) Tj ET"
    )
    result = project_document(
        request(
            source,
            "application/pdf",
            [
                {
                    "kind": "literal",
                    "pattern": "Monsieur Durand",
                    "replacement_mode": "initials",
                }
            ],
        )
    )
    assert result.match_count == 1
    assert "Durand" not in PdfDocument.from_bytes(
        base64.b64decode(result.content_base64)
    ).extract_text(0)


@pytest.mark.parametrize("discretionary", [False, True])
def test_subset_font_hyphens_survive_missing_reverse_unicode_mapping(
    monkeypatch: pytest.MonkeyPatch,
    discretionary: bool,
) -> None:
    import ctypes

    original = pdfium.raw.FPDFText_SetText

    def missing_hyphen_mapping(*arguments: object) -> int:
        text = ctypes.cast(arguments[1], ctypes.POINTER(ctypes.c_ushort))
        if text[0] == 45:
            return 1
        return original(*arguments)

    monkeypatch.setattr(pdfium.raw, "FPDFText_SetText", missing_hyphen_mapping)
    content = (
        b"BT /F1 12 Tf 72 705 Td (Monsieur Durand cite une proce-) Tj 0 -14 Td (dure distincte.) Tj ET"
        if discretionary
        else b"BT /F1 12 Tf 72 705 Td (Monsieur Durand cite une sous-section.) Tj ET"
    )
    source = pdf_fixture(content)
    result = project_document(
        request(
            source,
            "application/pdf",
            [
                {
                    "kind": "literal",
                    "pattern": "Monsieur Durand",
                    "replacement_mode": "initials",
                }
            ],
        )
    )
    assert result.match_count == 1


def test_pdfium_failed_insert_does_not_destroy_an_already_transferred_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from galadril_scribe.redaction_engine import _glyphs, _key
    from galadril_scribe.redaction_pdf import remove_pdf_glyphs

    source = pdf_fixture(
        b"BT /F1 12 Tf 72 705 Td (Durand keeps this text.) Tj ET"
    )
    with pdfium.PdfDocument(source) as native:
        glyphs = _glyphs(native, 0)
    selected = {_key(glyph) for glyph in glyphs[:6]}
    destroyed: list[object] = []
    original_destroy = pdfium.raw.FPDFPageObj_Destroy

    def transferred_failure(page: object, obj: object) -> int:
        original_destroy(obj)
        return 0

    def track_double_destroy(obj: object) -> None:
        destroyed.append(obj)

    monkeypatch.setattr(
        pdfium.raw, "FPDFPage_InsertObject", transferred_failure
    )
    monkeypatch.setattr(pdfium.raw, "FPDFPageObj_Destroy", track_double_destroy)
    with pytest.raises(ValueError):
        remove_pdf_glyphs(source, [selected], [])
    assert not destroyed


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
