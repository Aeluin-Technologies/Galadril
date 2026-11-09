"""Federate public legal discovery without treating search hits as citations."""

from __future__ import annotations

import asyncio
import os
import re
from datetime import date
from urllib.parse import urlsplit

import httpx
from opentelemetry import trace
from pydantic import JsonValue

from galadril_mcp_service.providers.piste_api import (
    Fund,
    PisteUnavailable,
    _response_object,
    read_private_credential,
)

_ORIGIN = "https://librejustice.fr"
_OFFICIAL_ID = re.compile(r"(?:CETATEXT|JURITEXT|LEGIARTI|JORFTEXT)\d{12}")
_NORMS = frozenset({"CODE_DATE", "LODA_DATE", "JORF"})
_COURTS = {"CETAT": ("CE", "CAA", "TA"), "JURI": ("CC", "CA", "TJ", "TCOM")}


def _candidate(value: JsonValue) -> dict[str, JsonValue] | None:
    """Keep search leads bounded and avoid treating generated summaries as evidence."""
    if not isinstance(value, dict):
        return None
    title, url = value.get("title"), value.get("url")
    if (
        not isinstance(title, str)
        or not isinstance(url, str)
        or len(url) > 2048
    ):
        return None
    parsed = urlsplit(url)
    if url.startswith("/") and not url.startswith("//"):
        url = _ORIGIN + url
    elif parsed.netloc != "librejustice.fr" or parsed.scheme != "https":
        return None
    hit: dict[str, JsonValue] = {"title": title[:512], "url": url}
    for key, limit in (
        ("snippet", 1200),
        ("date", 10),
        ("seat", 64),
        ("source", 128),
        ("valid_from", 10),
        ("valid_to", 10),
    ):
        item = value.get(key)
        if isinstance(item, str):
            hit[key] = item[:limit]
    numbers = value.get("docket_numbers")
    if isinstance(numbers, list):
        hit["docket_numbers"] = [
            number[:128] for number in numbers[:8] if isinstance(number, str)
        ]
    official = value.get("official_url")
    if isinstance(official, str) and len(official) <= 2048:
        target = urlsplit(official)
        if target.scheme == "https" and target.netloc in {
            "www.legifrance.gouv.fr",
            "legifrance.gouv.fr",
        }:
            hit["official_url"] = official
    if identifier := _identifier(hit):
        hit["legifrance_id"] = identifier
    return hit


def _identifier(value: dict[str, JsonValue]) -> str | None:
    """Only canonical document identities establish a cross-provider duplicate."""
    for key in ("id", "official_url", "url", "source"):
        candidate = value.get(key)
        if isinstance(candidate, str) and (
            match := _OFFICIAL_ID.search(candidate)
        ):
            return match.group()
    titles = value.get("titles")
    if isinstance(titles, list) and len(titles) == 1:
        title = titles[0]
        if isinstance(title, dict):
            return _identifier(
                {key: title[key] for key in ("id", "url") if key in title}
            )
    return None


async def search_librejustice(
    http: httpx.AsyncClient,
    query: str,
    fund: Fund,
    at_date: date | None,
    page: int,
) -> list[dict[str, JsonValue]] | None:
    """Send only a public research query to a fixed origin using its own API key."""
    offset = (page - 1) * 10
    if offset > 99:
        return None
    params = [("query", query), ("limit", "10"), ("offset", str(offset))]
    if fund in _NORMS:
        path = "/api/search-textes"
        params.append(("jurisdiction", "FR"))
        if at_date is not None:
            params.append(("as_of", at_date.isoformat()))
    else:
        path = "/api/search"
        params.append(("ai_rerank", "true"))
        params.extend(
            ("jurisdiction_type", court) for court in _COURTS.get(fund, ())
        )
    with trace.get_tracer("galadril.mcp").start_as_current_span(
        "mcp.librejustice.search",
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            location = os.environ.get("LIBREJUSTICE_API_KEY_FILE")
            key = (
                await asyncio.to_thread(read_private_credential, location)
                if location
                else os.environ.get("LIBREJUSTICE_API_KEY", "").strip()
            )
            if not key:
                return None
            async with (
                asyncio.timeout(15),
                http.stream(
                    "GET",
                    _ORIGIN + path,
                    params=params,
                    headers={"Authorization": "Bearer " + key},
                    timeout=12,
                    follow_redirects=False,
                ) as response,
            ):
                span.set_attribute(
                    "http.response.status_code", response.status_code
                )
                if response.status_code != 200:
                    return None
                payload = await _response_object(response)
            hits = payload.get("hits")
            if isinstance(hits, list):
                return [
                    hit
                    for item in hits[:10]
                    if (hit := _candidate(item)) is not None
                ]
        except (httpx.HTTPError, PisteUnavailable, TimeoutError, ValueError):
            span.set_attribute("mcp.provider.unavailable", True)
    return None


def shortlist(
    values: list[JsonValue], seen: set[str], *, overlap: list[str] | None = None
) -> list[JsonValue]:
    """Keep provider order, replacing proven duplicates with the next ranked hit."""
    selected: list[JsonValue] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        key = _identifier(value)
        url = value.get("url")
        identity = key or (url if isinstance(url, str) else None)
        if identity is not None:
            if identity in seen:
                if (
                    overlap is not None
                    and key is not None
                    and key not in overlap
                ):
                    overlap.append(key)
                continue
            seen.add(identity)
        selected.append(value)
        if len(selected) == 5:
            break
    return selected
