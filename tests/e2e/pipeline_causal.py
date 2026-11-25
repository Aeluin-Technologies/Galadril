"""Bounded cohort and causal lineage against real Vision and Amarth."""

from __future__ import annotations

import asyncio
import json

from assertions import (
    eventually,
)
from clients import (
    OUTSIDER_ID,
    TENANT_ID,
    UPLOADER_ID,
    GatewayClient,
    PipelineExecutionError,
    SpiceDBProbe,
    capture_tempo_failure,
    consume_lineage,
    publish_causal_command,
    read_causal_cohort,
    read_causal_state,
    statuses_by_step,
    upload_presigned,
)
from gateway_scenario import _field, _string

_CAUSAL_TRACE_ID = "42124a3e76ba4ef19303cb50267a8c2d"
_CAUSAL_LINEAGE_TIMEOUT_SECONDS = 600.0


async def exercise_causal_branch(
    gateway: GatewayClient,
    spicedb: SpiceDBProbe,
    token: str,
) -> None:
    """Runs real Amarth on Gateway observations resolved by native LI-ESKG."""
    first_source_key: str | None = None
    for index in range(36):
        cohort = "alpha" if index < 30 else "decoy"
        signal = (((index * 7) % 13) + 1) / 14.0
        preceding = ((((index - 1) * 7) % 13) + 1) / 14.0
        content = json.dumps(
            {
                "cohort": cohort,
                "signal": signal,
                "outcome": preceding,
                "decoy": index >= 30,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        name = f"e2e-record-{index:02d}.txt"
        staged = _field(
            await gateway.execute(
                token,
                "mutation { requestStagingUpload { uploadUrl stagingKey } }",
                trace_id=_CAUSAL_TRACE_ID if index == 0 else None,
            ),
            "requestStagingUpload",
        )
        await upload_presigned(_string(staged, "uploadUrl"), content)
        promoted = await gateway.execute(
            token,
            """
            mutation Promote($key: String!, $name: String!) {
              completeUpload(stagingKey: $key, targetName: $name)
            }
            """,
            {"key": _string(staged, "stagingKey"), "name": name},
            trace_id=_CAUSAL_TRACE_ID if index == 0 else None,
        )
        raw_key = _string(promoted, "completeUpload")
        nonce = _string(staged, "stagingKey").rsplit("/", 1)[-1]
        assert raw_key == f"{TENANT_ID}/raw/default/{nonce}/{name}"
        if index == 0:
            first_source_key = raw_key
        await asyncio.sleep(1.0)
    alpha_entity = await eventually(
        read_causal_cohort,
        timeout_seconds=1800.0,
        description="30 concordant and 6 decoy Gateway observations",
        abort_on=(PipelineExecutionError,),
    )
    assert await spicedb.allowed(
        resource_type="entity_state",
        resource_id=f"{TENANT_ID}/{alpha_entity}",
        permission="view",
        user_id=UPLOADER_ID,
    )
    assert not await spicedb.allowed(
        resource_type="entity_state",
        resource_id=f"{TENANT_ID}/{alpha_entity}",
        permission="view",
        user_id=OUTSIDER_ID,
    )
    if first_source_key is None:
        raise AssertionError("The causal fixture has no admitted source")
    correlation_id = await publish_causal_command(
        alpha_entity,
        source_key=first_source_key,
        trace_id=_CAUSAL_TRACE_ID,
    )
    async with capture_tempo_failure(_CAUSAL_TRACE_ID):
        causal = await eventually(
            lambda: read_causal_state(alpha_entity),
            timeout_seconds=300.0,
            description="real Amarth causal analysis of Gateway-derived entity",
            abort_on=(PipelineExecutionError,),
        )
    assert causal.get("observation_count", 0) >= 30
    assert causal.get("entity_scope_size") == 1
    assert causal.get("event_scope_size", 0) >= 30
    assert isinstance(causal.get("causal_links"), int)
    assert causal["causal_links"] >= 1
    assert isinstance(causal.get("validated_effects"), int)
    assert causal.get("counterfactual_ready") is True
    async with capture_tempo_failure(_CAUSAL_TRACE_ID):
        lineage = await consume_lineage(
            {"causal": frozenset({"running", "completed"})},
            correlation_id,
            _CAUSAL_LINEAGE_TIMEOUT_SECONDS,
        )
    assert statuses_by_step(lineage).get("causal") == {
        "running",
        "completed",
    }
