"""Runs data-derived benchmark oracles against a freshly published pipeline."""

from __future__ import annotations

from authentication import login_user
from clients import (
    OUTSIDER_ID,
    UPLOADER_ID,
)
from empirical_causal import exercise_empirical_causal
from empirical_pipeline import exercise_empirical_pipeline
from gateway_scenario import GatewayScenario
from retrieval_benchmark import load_benchmark
from retrieval_lifecycle import exercise_retrieval


async def run_empirical_scenarios(
    scenario: GatewayScenario, *, full: bool
) -> None:
    """Materializes the retail identities consumed by the retrieval oracle."""
    retrieval = load_benchmark()
    await exercise_empirical_pipeline(
        scenario.gateway,
        scenario.spicedb,
        full=full,
        required_sources={
            (document.customer_id, document.source_row)
            for document in retrieval.documents
        },
    )
    await exercise_empirical_causal(
        scenario.gateway, scenario.spicedb, full=full
    )
    # Empirical waits can outlast the Gateway session.
    await login_user(UPLOADER_ID)
    await login_user(OUTSIDER_ID)
    await exercise_retrieval(scenario.gateway, retrieval)
