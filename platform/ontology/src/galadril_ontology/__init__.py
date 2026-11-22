"""Transport-only GOL projections and admitted Gateway runtime operations."""

from .client import GatewayGolClient
from .contracts import (
    AuthorizedSymbol,
    GolBlockContract,
    GolEffect,
    GolHandoff,
    GolLease,
    GolProjection,
    GolRequest,
    GolSymbolKind,
)
from .errors import GolDenied, GolError, GolUnavailable
from .identity import normalize_tenant_id, require_same_tenant, source_object_id
from .runtime import (
    GolRuntimeManager,
    active_gol_lease,
    make_gol_state_effect,
    protected_gol_effect,
    protected_gol_effects,
    protected_gol_state_effect,
    scoped_instance_id,
)

__all__ = [
    "AuthorizedSymbol",
    "GatewayGolClient",
    "GolBlockContract",
    "GolEffect",
    "GolHandoff",
    "GolDenied",
    "GolError",
    "GolLease",
    "GolProjection",
    "GolRequest",
    "GolRuntimeManager",
    "GolSymbolKind",
    "GolUnavailable",
    "active_gol_lease",
    "normalize_tenant_id",
    "protected_gol_effect",
    "protected_gol_effects",
    "protected_gol_state_effect",
    "make_gol_state_effect",
    "scoped_instance_id",
    "require_same_tenant",
    "source_object_id",
]
