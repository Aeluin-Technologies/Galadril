"""Composes an actor-local client of the admitted Gateway GOL reader."""

from galadril_ontology import GatewayGolClient, GolRuntimeManager


def build_vision_gol_runtime(
    endpoint: str, *, worker_key_path: str | None = None
) -> GolRuntimeManager:
    """Keeps storage credentials and canonical GOL models outside Ray actors."""
    return GolRuntimeManager(
        GatewayGolClient(endpoint, worker_key_path=worker_key_path)
    )
