"""Federate public legal discovery without treating search hits as citations."""

from __future__ import annotations

import asyncio
import os
import re
from datetime import date
from time import monotonic
from urllib.parse import quote, unquote, urlsplit

import httpx
from galadril_mcp_core.legal import NormRead
from opentelemetry import metrics, trace
from pydantic import BaseModel, JsonValue

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
_SEGMENT = re.compile(r"[\w.~:-]{1,256}")
_LEGIFRANCE_NORM_ID = re.compile(
    r"(?:LEGI|JORF|KALI|CIRC)(?:ARTI|TEXT|SCTA)\d{12}"
)
_REJECTIONS = {
    400: "invalid_filters",
    404: "not_found",
    422: "invalid_filters",
    429: "rate_limited",
}
_DURATION = metrics.get_meter("galadril.mcp").create_histogram(
    "galadril.mcp.librejustice.duration", unit="s"
)


def _segment(value: str) -> str:
    if value in {".", ".."} or not _SEGMENT.fullmatch(value):
        raise ValueError("Invalid LibreJustice source address")
    return quote(value, safe="")


def _public_path(url: str, kind: str) -> str:
    """Parse an identifier from a provider URL without using it as a destination."""
    parsed = urlsplit(url)
    if (
        (parsed.scheme, parsed.netloc)
        not in {("https", "librejustice.fr"), ("", "")}
        or not parsed.path.startswith(f"/{kind}/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Expected a LibreJustice source URL")
    parts = parsed.path.strip("/").split("/")
    return "/".join(_segment(unquote(part)) for part in parts)


def decision_path(url: str) -> str:
    """Resolve a decision link to the REST read route on the fixed origin."""
    return "/api/decision/" + _public_path(url, "decision").rsplit("/", 1)[-1]


def search_params(request: BaseModel) -> list[tuple[str, str]]:
    """Keep repeated filters separate so LibreJustice applies its native OR logic."""
    params: list[tuple[str, str]] = []
    for key, value in request.model_dump(
        mode="json", exclude_none=True
    ).items():
        if isinstance(value, list):
            params.extend((key, str(item)) for item in value)
        elif isinstance(value, bool):
            params.append((key, "true" if value else "false"))
        elif value != "":
            params.append((key, str(value)))
    return params


async def _api_key() -> str:
    location = os.environ.get("LIBREJUSTICE_API_KEY_FILE")
    if location:
        return await asyncio.to_thread(read_private_credential, location)
    return os.environ.get("LIBREJUSTICE_API_KEY", "").strip()


async def read_public(
    http: httpx.AsyncClient,
    path: str,
    params: list[tuple[str, str]],
    *,
    operation: str,
) -> dict[str, JsonValue]:
    """Read bounded public evidence without passing through any Galadril authority."""
    started = monotonic()
    outcome = "unavailable"
    with trace.get_tracer("galadril.mcp").start_as_current_span(
        "mcp.librejustice.request",
        attributes={"mcp.provider.operation": operation},
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            async with asyncio.timeout(15):
                key = await _api_key()
                if key:
                    for _ in range(2):
                        async with http.stream(
                            "GET",
                            _ORIGIN + path,
                            params=params,
                            headers={"Authorization": "Bearer " + key},
                            timeout=12,
                            follow_redirects=False,
                        ) as response:
                            status = response.status_code
                            span.set_attribute(
                                "http.response.status_code", status
                            )
                            if status == 200:
                                result = await _response_object(response)
                                outcome = "ok"
                                return result
                            if rejection := _REJECTIONS.get(status):
                                outcome = rejection
                                return {"status": rejection}
                            location = response.headers.get("location", "")
                            if (
                                status == 308
                                and path.startswith("/api/article/")
                                and location.startswith("/api/norm/")
                            ):
                                path = "/api/" + _public_path(
                                    urlsplit(location).path.removeprefix(
                                        "/api"
                                    ),
                                    "norm",
                                )
                                continue
                            break
        except (httpx.HTTPError, PisteUnavailable, TimeoutError, ValueError):
            span.add_event("mcp.librejustice.request_failed")
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            span.set_attribute("mcp.provider.outcome", outcome)
            _DURATION.record(
                monotonic() - started,
                {"operation": operation, "outcome": outcome},
            )
        return {"status": "unavailable"}


async def read_norm(
    http: httpx.AsyncClient, request: NormRead
) -> dict[str, JsonValue]:
    """Resolve a dated norm using its canonical link or public source identifier."""
    params = [("as_of", request.as_of.isoformat())] if request.as_of else []
    if request.url is not None:
        path = "/api/" + _public_path(request.url, "norm")
    elif request.code is not None:
        path = "/api/norm/" + _segment(request.code)
    elif request.text_id is not None:
        identifier = request.text_id
        if identifier.startswith("https://"):
            parsed = urlsplit(identifier)
            if parsed.netloc not in {
                "www.legifrance.gouv.fr",
                "legifrance.gouv.fr",
            }:
                raise ValueError("Expected a Légifrance permalink")
            identifier = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        if identifier.startswith("BOI-"):
            path = "/api/norm/" + _segment(identifier)
        elif _LEGIFRANCE_NORM_ID.fullmatch(identifier):
            path = "/api/article/" + _segment(identifier)
        else:
            raise ValueError("Expected a Légifrance or BOFiP identifier")
        if request.article is not None and path.startswith("/api/article/"):
            root = await read_public(http, path, params, operation="get_norm")
            url = root.get("url")
            if not isinstance(url, str):
                return root
            path = "/api/" + _public_path(url, "norm")
    else:
        raise ValueError("A public norm address is required")
    if request.article is not None:
        path += "/" + _segment(request.article)
    return await read_public(http, path, params, operation="get_norm")


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
            key = await _api_key()
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
