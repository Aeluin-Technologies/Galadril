"""Verify source identity and literal support without inferring legal relevance."""

import re
import unicodedata
from datetime import UTC, date, datetime
from html import unescape

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from galadril_mcp_service.providers.piste_api import Provider


class Citation(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True
    )
    provider: Provider
    identifier: str = Field(min_length=1, max_length=96)
    number: str = Field(min_length=1, max_length=128)
    court: str = Field(min_length=1, max_length=256)
    date: date
    passage: str = Field(min_length=1, max_length=8192)


def _normalize(value: str) -> str:
    """Ignore presentation differences without paraphrasing the source text."""
    return " ".join(
        unicodedata.normalize("NFC", unescape(re.sub(r"<[^>]*>", " ", value)))
        .replace("’", "'")
        .split()
    ).casefold()


def _decision_date(value: JsonValue) -> date | None:
    """Read provider dates without guessing a date from an invalid timestamp."""
    try:
        if isinstance(value, (float, int)):
            return datetime.fromtimestamp(value / 1000, UTC).date()
        if isinstance(value, str):
            return date.fromisoformat(value[:10])
    except (ValueError, OverflowError, OSError):
        return None
    return None


def _source_matches(citation: Citation, record: dict[str, JsonValue]) -> bool:
    """Require a fetched official document, rather than a search-result summary."""
    return (
        record.get("operation") == "consult"
        and record.get("provider") == citation.provider
        and record.get("id") == citation.identifier
    )


def _identity_matches(citation: Citation, raw: dict[str, JsonValue]) -> bool:
    """Match each identity field independently of any quoted passage."""
    court = raw.get("juridiction", raw.get("jurisdiction"))
    if not isinstance(court, str):
        return False
    return (
        raw.get("id") == citation.identifier
        and raw.get("num", raw.get("number")) == citation.number
        and _normalize(court) == _normalize(citation.court)
        and _decision_date(raw.get("dateTexte", raw.get("decision_date")))
        == citation.date
    )


def _supports_passage(citation: Citation, raw: dict[str, JsonValue]) -> bool:
    """Reject empty normalized quotes, which otherwise match every source."""
    text = raw.get("texte", raw.get("text"))
    passage = _normalize(citation.passage)
    return (
        bool(passage) and isinstance(text, str) and passage in _normalize(text)
    )


def verify_reference(
    citation: Citation, record: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    """Verify literal support without certifying relevance or legal applicability.

    Args:
        citation: Claimed official identity and exact quotation.
        record: Envelope returned by the fixed-origin official API adapter.

    Returns:
        Separate identity and quotation checks, with legal review still required.
    """
    data = record.get("data")
    raw = (
        data.get("text")
        if isinstance(data, dict) and citation.provider == "legifrance"
        else data
    )
    identity = False
    passage = False
    if isinstance(raw, dict) and _source_matches(citation, record):
        identity = _identity_matches(citation, raw)
        passage = identity and _supports_passage(citation, raw)
    return {
        "identity_verified": identity,
        "passage_verified": passage,
        "source_sha256": record.get("sha256"),
        "applicability_verified": False,
        "review_required": "Temporal/material scope, authoritative grounds versus summaries, and application to verified facts require separate legal review.",
    }
