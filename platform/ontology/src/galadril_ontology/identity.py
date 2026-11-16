"""Tenant identity and exact GOL transport selector validation."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_TENANT_ID_MAX_LEN = 128
_TENANT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SYMBOL_KEY_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*::[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$"
)


def normalize_tenant_id(value: object) -> str:
    """Normalizes the fail-closed tenant key shared by platform stores."""
    if not isinstance(value, str):
        raise ValueError("tenant_id must be a string")
    tenant_id = value.strip()
    if tenant_id.startswith("tenant:"):
        _, tenant_id = tenant_id.split(":", 1)
        tenant_id = tenant_id.strip()
    if not tenant_id:
        raise ValueError("tenant_id is required")
    if len(tenant_id) > _TENANT_ID_MAX_LEN:
        raise ValueError("tenant_id exceeds maximum length")
    if _TENANT_ID_RE.fullmatch(tenant_id) is None:
        raise ValueError("tenant_id contains unsupported characters")
    return tenant_id


def require_same_tenant(expected: object, actual: object) -> str:
    """Returns the normalized tenant only when both scopes are identical."""
    expected_tenant = normalize_tenant_id(expected)
    actual_tenant = normalize_tenant_id(actual)
    if expected_tenant != actual_tenant:
        raise ValueError(
            f"tenant mismatch: expected {expected_tenant}, got {actual_tenant}"
        )
    return expected_tenant


def validate_symbol_key(value: str) -> str:
    """Checks transport spelling without performing semantic resolution."""
    if len(value) > 512 or _SYMBOL_KEY_RE.fullmatch(value) is None:
        raise ValueError("Invalid GOL symbol selector")
    return value


def source_object_id(storage_path: object, tenant_id: str) -> str:
    """Preserves the exact S3 key spelling bound by Gateway finalization."""
    if not isinstance(storage_path, str) or len(storage_path) > 4096:
        raise ValueError("GOL source object unavailable")
    uri = urlsplit(storage_path)
    key = uri.path.removeprefix("/")
    if (
        uri.scheme != "s3"
        or not uri.netloc
        or uri.username
        or uri.password
        or uri.query
        or uri.fragment
        or not key.startswith(normalize_tenant_id(tenant_id) + "/")
    ):
        raise ValueError("GOL source object unavailable")
    return "raw:" + key
