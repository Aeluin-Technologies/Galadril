"""Randomized-trial estimate from Gateway-ingested, Vision-persisted evidence."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping

import networkx as nx
import pandas as pd
import psycopg
import pytest
from amarth.estimation.dowhy import DowhyEstimator
from assertions import eventually, require_mapping
from clients import (
    POSTGRES_DSN,
    TENANT_ID,
    GatewayClient,
    SpiceDBProbe,
    canonical_spicedb_object_id,
    mint_token,
    upload_presigned,
)
from empirical_data import load_trial_rows, trial_estimate
from scenario import ADMIN, encode_trial_rows


async def _persisted_trial() -> (
    tuple[tuple[str, str, str, Mapping[str, object]], ...] | None
):
    """Waits for the complete cross-sectional cohort and replicated lineage."""
    async with await psycopg.AsyncConnection.connect(
        POSTGRES_DSN
    ) as connection:
        cursor = await connection.execute(
            """
            SELECT entity_id, event_id, state_value->>'source_field', state_value
            FROM entity_states
            WHERE tenant_id = %s AND state_value->>'label' = 'trial-participant'
            """,
            (TENANT_ID,),
        )
        rows = await cursor.fetchall()
        outbox = await connection.execute(
            "SELECT COUNT(*) FROM authz_outbox WHERE tenant_id = %s",
            (TENANT_ID,),
        )
        outbox_row = await outbox.fetchone()
    if len(rows) < 205 or outbox_row is None or int(outbox_row[0]) != 0:
        return None
    assert len(rows) == 205, "Vision duplicated randomized-trial rows"
    return tuple(
        (
            str(entity),
            str(event),
            str(source),
            require_mapping(state, "trial state"),
        )
        for entity, event, source, state in rows
    )


async def exercise_empirical_causal(
    gateway: GatewayClient, spicedb: SpiceDBProbe
) -> None:
    """Runs Amarth over the exact persisted assignment and outcome observations."""
    print("E2E stage: uploading randomized-trial observations", flush=True)
    token = mint_token(ADMIN)
    staged = require_mapping(
        (
            await gateway.execute(
                token,
                'mutation { requestStagingUpload(groupId: "trial") { uploadUrl stagingKey } }',
            )
        ).get("requestStagingUpload"),
        "trial staging",
    )
    upload_url = staged.get("uploadUrl")
    staging_key = staged.get("stagingKey")
    assert isinstance(upload_url, str) and isinstance(staging_key, str)
    rows = load_trial_rows()
    await upload_presigned(upload_url, encode_trial_rows(rows))
    promoted = await gateway.execute(
        token,
        """
        mutation PromoteTrial($key: String!) {
          completeUpload(stagingKey: $key, targetName: "trial-205.csv", groupId: "trial")
        }
        """,
        {"key": staging_key},
    )
    raw_key = f"{TENANT_ID}/raw/trial/trial-205.csv"
    assert promoted.get("completeUpload") == raw_key
    persisted = await eventually(
        _persisted_trial,
        timeout_seconds=1200.0,
        description="205 randomized-trial Vision states and drained authz outbox",
    )
    expected = {row.record_id: row for row in rows}
    observed: dict[int, tuple[float, float]] = {}
    event_ids: set[str] = set()
    for _, event_id, source, state in persisted:
        assert source.startswith("trial:")
        record_id = int(source.removeprefix("trial:"))
        assert record_id not in observed
        row = expected[record_id]
        evidence = require_mapping(
            state.get("scalar_evidence"), "trial metrics"
        )
        treatment = float(evidence["treatment"])
        outcome = float(evidence["outcome"])
        assert treatment == (1.0 if row.condition == 1 else 0.0)
        assert outcome == row.units_selected
        observed[record_id] = (treatment, outcome)
        event_ids.add(event_id)
    assert observed.keys() == expected.keys()
    assert len(event_ids) == 205
    source_map = await spicedb.event_sources()
    assert all(
        source_map.get(canonical_spicedb_object_id(f"{TENANT_ID}/{event_id}"))
        == canonical_spicedb_object_id(raw_key)
        for event_id in event_ids
    )
    frame = pd.DataFrame(
        {
            "treatment": [observed[index][0] for index in sorted(observed)],
            "outcome": [observed[index][1] for index in sorted(observed)],
        }
    )
    graph = nx.DiGraph([("treatment", "outcome")])
    print("E2E stage: estimating randomized effect with Amarth", flush=True)
    estimate = await asyncio.to_thread(
        DowhyEstimator(refutation_simulations=0).estimate_effect,
        frame,
        graph,
        "treatment",
        "outcome",
    )
    assert estimate is not None
    assert math.isfinite(estimate.ate)
    independent = trial_estimate(rows)
    assert estimate.ate == pytest.approx(independent.difference, abs=0.01)
    assert independent.lower < 0 < independent.upper
