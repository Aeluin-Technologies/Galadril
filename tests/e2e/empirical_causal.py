"""Randomized-trial estimate from Gateway-ingested, Vision-persisted evidence."""

from __future__ import annotations

import asyncio
import math
from time import monotonic

import networkx as nx
import pandas as pd
import pytest
from amarth.estimation.dowhy import DowhyEstimator
from assertions import eventually, require_mapping
from authentication import login_user
from clients import (
    TENANT_ID,
    GatewayClient,
    PipelineExecutionError,
    SpiceDBProbe,
    canonical_spicedb_object_id,
    read_trial_states,
    upload_presigned,
)
from empirical_data import load_trial_rows, trial_estimate
from environment import EMPIRICAL_STATE_TIMEOUT_SECONDS
from scenario import ADMIN, encode_trial_rows


async def exercise_empirical_causal(
    gateway: GatewayClient, spicedb: SpiceDBProbe, *, full: bool
) -> None:
    """Runs Amarth over the exact persisted assignment and outcome observations."""
    print("E2E stage: uploading randomized-trial observations", flush=True)
    started = monotonic()
    token = await login_user(ADMIN)
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
    if not full:
        rows = (
            tuple(row for row in rows if row.condition == 1)[:12]
            + tuple(row for row in rows if row.condition == 6)[:12]
        )
    target = f"trial-{len(rows)}.csv"
    await upload_presigned(upload_url, encode_trial_rows(rows))
    promoted = await gateway.execute(
        token,
        """
        mutation PromoteTrial($key: String!, $target: String!) {
          completeUpload(stagingKey: $key, targetName: $target, groupId: "trial")
        }
        """,
        {"key": staging_key, "target": target},
    )
    nonce = staging_key.rsplit("/", 1)[-1]
    raw_key = f"{TENANT_ID}/raw/trial/{nonce}/{target}"
    assert promoted.get("completeUpload") == raw_key
    persisted = await eventually(
        lambda: read_trial_states(len(rows)),
        timeout_seconds=EMPIRICAL_STATE_TIMEOUT_SECONDS,
        description=f"{len(rows)} randomized-trial Vision states and drained authz outbox",
        abort_on=(PipelineExecutionError,),
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
    assert len(event_ids) == len(rows)
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
    if full:
        assert independent.lower < 0 < independent.upper
    print(
        f"E2E trial: {len(rows)} observations and causal estimate in {monotonic() - started:.1f}s",
        flush=True,
    )
