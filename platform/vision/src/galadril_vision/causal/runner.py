"""Vision adapter for time-windowed Amarth inference over ESKG observations."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import orjson
import structlog
from amarth.observations import (
    CausalLink,
    GraphRelationshipObservation,
    Observation,
    ObservationWindow,
)
from amarth.router import AmarthRouter
from galadril_vision.common.exceptions import GaladrilVisionError
from galadril_vision.common.types import normalize_tenant_id
from galadril_vision.connectors.postgres.client import PostgresClient
from galadril_vision.connectors.postgres.graph import GraphStore

logger = structlog.get_logger(__name__)


class CausalJobError(GaladrilVisionError):
    """Raised when a causal job fails."""


@dataclass(frozen=True, slots=True)
class MetricMapping:
    """Retains compatibility with version-one causal pipeline configuration."""

    metric_id: str
    expr_sql: str
    agg_sql: str


@dataclass(frozen=True, slots=True)
class CausalSliceSpec:
    """Bounds one tenant-isolated ESKG observation slice."""

    target: str
    lookback: timedelta
    bucket: str
    max_events: int
    max_states: int
    state_metrics: tuple[MetricMapping, ...]
    k_min: int
    k_max: int
    max_vertices: int
    include_presence_links: bool


_ALLOWED_ESKG_RELATIONSHIPS: tuple[str, ...] = (
    "TRIGGERS",
    "LEADS_TO",
    "EVOLUTION",
    "CONTAIN",
    "INFLUENCE",
    "OCCUR",
    "DERIVED_FROM",
    "MENTIONS",
    "DESCRIBES",
    "CAUSES",
)

_PRESENCE_PIVOT_RELATIONSHIPS: tuple[str, ...] = (
    "APPEARS_IN",
    "PARTICIPATED_IN",
    "DERIVED_FROM",
    "MENTIONS",
)

_SIMPLE_PROPERTY_TYPES = (str, float, int, bool, type(None))


def _parse_lookback(value: str | None) -> timedelta:
    """Parses a bounded causal lookback without accepting ambiguous units."""
    if not value:
        return timedelta(days=7)
    normalized = value.strip().lower()
    units = {
        "s": timedelta(seconds=1),
        "m": timedelta(minutes=1),
        "h": timedelta(hours=1),
        "d": timedelta(days=1),
    }
    unit = units.get(normalized[-1:])
    if unit is None:
        raise ValueError(f"Unsupported lookback format: '{value}'")
    amount = int(normalized[:-1])
    if amount <= 0:
        raise ValueError("lookback amount must be positive")
    return unit * amount


def _bucket_timedelta(value: str | None) -> timedelta:
    """Converts a compact bucket into an exact observation cadence."""
    if not value:
        return timedelta(hours=1)
    normalized = value.strip().lower()
    aliases = {
        "second": "s",
        "seconds": "s",
        "minute": "m",
        "minutes": "m",
        "hour": "h",
        "hours": "h",
        "day": "d",
        "days": "d",
    }
    parts = normalized.split()
    if len(parts) == 2 and parts[1] in aliases:
        normalized = f"{parts[0]}{aliases[parts[1]]}"
    return _parse_lookback(normalized)


def _make_cache_key(payload: dict[str, Any]) -> str:
    """Creates a deterministic inference identity for idempotent graph writes."""
    raw = orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(raw).hexdigest()


def _parse_entity_target(target: str) -> str | None:
    if not target.startswith("entity:"):
        return None
    entity_id = target.split("entity:", 1)[1].strip()
    return entity_id or None


def _default_state_metrics_v1() -> tuple[MetricMapping, ...]:
    return (
        MetricMapping(
            metric_id="state_avg_confidence",
            expr_sql="(state_value->>'confidence')::double precision",
            agg_sql="AVG",
        ),
        MetricMapping(
            metric_id="state_count",
            expr_sql="1::double precision",
            agg_sql="COUNT",
        ),
    )


async def _cache_get(
    client: PostgresClient, tenant_id: str, cache_key: str
) -> dict[str, Any] | None:
    async with client.tenant_connection(tenant_id) as conn:
        result = await conn.execute(
            """
            SELECT cache_key, status, result_summary
            FROM causal_runs
            WHERE tenant_id = $1 AND cache_key = $2
            """,
            (tenant_id, cache_key),
        )
        row = await result.fetchone()
    if not row:
        return None
    summary = row[2]
    if isinstance(summary, str):
        try:
            summary = orjson.loads(summary)
        except orjson.JSONDecodeError:
            summary = {}
    return {
        "cache_key": row[0],
        "status": row[1],
        "result_summary": summary,
    }


async def _cache_put(
    client: PostgresClient,
    *,
    tenant_id: str,
    cache_key: str,
    target: str,
    window_start: datetime,
    window_end: datetime,
    status: str,
    result_summary: dict[str, Any],
) -> None:
    async with client.tenant_connection(tenant_id) as conn:
        await conn.execute(
            """
            INSERT INTO causal_runs (
                tenant_id, cache_key, target, window_start, window_end,
                status, result_summary
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            ON CONFLICT (tenant_id, cache_key) DO UPDATE SET
                created_at = NOW(),
                status = EXCLUDED.status,
                result_summary = EXCLUDED.result_summary,
                window_start = EXCLUDED.window_start,
                window_end = EXCLUDED.window_end,
                target = EXCLUDED.target
            """,
            (
                tenant_id,
                cache_key,
                target,
                window_start,
                window_end,
                status,
                orjson.dumps(result_summary).decode(),
            ),
        )


async def _load_state_rows(
    client: PostgresClient,
    *,
    tenant_id: str,
    entity_ids: Sequence[str],
    window_start: datetime,
    window_end: datetime,
    max_rows: int,
) -> tuple[tuple[datetime, str, Mapping[str, object], str, str], ...]:
    if not entity_ids:
        return ()
    async with client.tenant_connection(tenant_id) as conn:
        result = await conn.execute(
            """
            SELECT event_time, state_type, state_value, entity_id, event_id
            FROM entity_states
            WHERE event_time >= $1 AND event_time <= $2
              AND entity_id = ANY($3::text[])
            ORDER BY event_time ASC
            LIMIT $4
            """,
            (window_start, window_end, list(entity_ids), max_rows),
        )
        rows = await result.fetchall()
    return tuple(rows)


async def _load_embedding_rows(
    client: PostgresClient,
    *,
    tenant_id: str,
    entity_ids: Sequence[str],
    window_start: datetime,
    window_end: datetime,
    max_rows: int,
) -> tuple[tuple[datetime, str, object, str, Mapping[str, object], str], ...]:
    if not entity_ids:
        return ()
    async with client.tenant_connection(tenant_id) as conn:
        result = await conn.execute(
            """
            SELECT created_at, modality, embedding, entity_id, metadata, id
            FROM entity_embeddings
            WHERE created_at >= $1 AND created_at <= $2
              AND entity_id = ANY($3::text[])
            ORDER BY created_at ASC
            LIMIT $4
            """,
            (window_start, window_end, list(entity_ids), max_rows),
        )
        rows = await result.fetchall()
    return tuple(rows)


async def _load_event_rows(
    client: PostgresClient,
    *,
    tenant_id: str,
    event_ids: Sequence[str],
    window_start: datetime,
    window_end: datetime,
    max_rows: int,
) -> tuple[tuple[datetime, str, Mapping[str, object], str], ...]:
    if not event_ids:
        return ()
    async with client.tenant_connection(tenant_id) as conn:
        result = await conn.execute(
            """
            SELECT event_time, event_type, properties, event_id
            FROM eskg_events
            WHERE event_time >= $1 AND event_time <= $2
              AND event_id = ANY($3::text[])
            ORDER BY event_time ASC
            LIMIT $4
            """,
            (window_start, window_end, list(event_ids), max_rows),
        )
        rows = await result.fetchall()
    return tuple(rows)


def _coerce_vector(value: object) -> tuple[float, ...]:
    """Normalizes pgvector codecs while rejecting malformed dense evidence."""
    if isinstance(value, str):
        stripped = value.strip().removeprefix("[").removesuffix("]")
        if not stripped:
            return ()
        return tuple(float(part) for part in stripped.split(","))
    if isinstance(value, Iterable) and not isinstance(
        value, (str, bytes, Mapping)
    ):
        return tuple(float(part) for part in value)
    raise ValueError("unsupported embedding representation")


def _numeric_properties(values: Mapping[str, object]) -> dict[str, float]:
    """Selects only finite-compatible scalar evidence for causal discovery."""
    return {
        key: float(value)
        for key, value in values.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def _relationship_properties(
    values: Mapping[str, object],
) -> dict[str, str | float | int | bool | None]:
    return {
        key: value
        for key, value in values.items()
        if isinstance(value, _SIMPLE_PROPERTY_TYPES)
    }


def _relationship_timestamp(
    properties: Mapping[str, object],
    window_start: datetime,
    window_end: datetime,
) -> datetime:
    value = properties.get("timestamp") or properties.get("observed_at")
    if isinstance(value, datetime):
        parsed = value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return window_start
    elif not isinstance(value, datetime):
        return window_start
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=window_start.tzinfo or UTC)
    if not window_start <= parsed <= window_end:
        return window_start
    return parsed


def _build_observation_window(
    *,
    window_start: datetime,
    window_end: datetime,
    bucket_seconds: float,
    state_rows: Sequence[tuple[datetime, str, Mapping[str, object], str, str]],
    embedding_rows: Sequence[
        tuple[datetime, str, object, str, Mapping[str, object], str]
    ],
    relationship_rows: Sequence[tuple[str, str, str, Mapping[str, object]]],
    event_rows: Sequence[tuple[datetime, str, Mapping[str, object], str]] = (),
) -> ObservationWindow:
    """Builds Amarth's native contract from normalized relational and AGE rows."""
    observations: list[Observation] = []
    for observed_at, state_type, state_value, entity_id, event_id in state_rows:
        observations.append(
            Observation(
                observation_id=f"state:{event_id}:{state_type}",
                graph_node_id=entity_id,
                observed_at=observed_at,
                observation_type=state_type,
                scalar_values=_numeric_properties(state_value),
            )
        )
    for (
        observed_at,
        modality,
        vector,
        entity_id,
        metadata,
        row_id,
    ) in embedding_rows:
        state_type_value = metadata.get("state_type")
        observation_type = (
            state_type_value
            if isinstance(state_type_value, str) and state_type_value
            else f"{modality.title()}Embedding"
        )
        observations.append(
            Observation(
                observation_id=f"embedding:{row_id}",
                graph_node_id=entity_id,
                observed_at=observed_at,
                observation_type=observation_type,
                embeddings={f"{modality}_embedding": _coerce_vector(vector)},
            )
        )
    for observed_at, event_type, properties, event_id in event_rows:
        scalar_values = {"presence": 1.0, **_numeric_properties(properties)}
        observations.append(
            Observation(
                observation_id=f"event:{event_id}",
                graph_node_id=event_id,
                observed_at=observed_at,
                observation_type=event_type,
                scalar_values=scalar_values,
            )
        )

    relationships = tuple(
        GraphRelationshipObservation(
            source_node_id=source,
            target_node_id=target,
            relationship_type=relationship_type,
            observed_at=_relationship_timestamp(
                properties, window_start, window_end
            ),
            properties=_relationship_properties(properties),
        )
        for source, target, relationship_type, properties in relationship_rows
    )
    return ObservationWindow(
        start=window_start,
        end=window_end,
        bucket=timedelta(seconds=bucket_seconds),
        observations=tuple(observations),
        relationships=relationships,
    )


def _effect_index(effects: Sequence[object]) -> dict[tuple[str, str], object]:
    index: dict[tuple[str, str], object] = {}
    for effect in effects:
        treatment = getattr(effect, "treatment", None)
        outcome = getattr(effect, "outcome", None)
        if isinstance(treatment, str) and isinstance(outcome, str):
            index[(treatment, outcome)] = effect
    return index


class AmarthCausalRunner:
    """Extracts tenant-scoped ESKG evidence and persists inferred CAUSES edges."""

    def __init__(
        self,
        pg: PostgresClient,
        graph: GraphStore,
        tenant_id: str,
        spec: CausalSliceSpec | None = None,
        target_outcome: str | None = None,
        window_size: str | None = None,
    ) -> None:
        self._pg = pg
        self._graph = graph
        self._tenant_id = normalize_tenant_id(tenant_id)
        self.spec = spec
        self.target_outcome = target_outcome
        self.window_size = window_size

    async def run(
        self,
        *,
        spec: CausalSliceSpec | None = None,
        target_outcome: str | None = None,
        window_size: str | None = None,
    ) -> dict[str, Any]:
        """Runs bounded causal inference without blocking Vision's event loop."""
        effective_spec = spec or self.spec
        effective_outcome = target_outcome or self.target_outcome
        effective_window = window_size or self.window_size
        if effective_spec is None or not effective_outcome:
            raise CausalJobError(
                "Incomplete configuration context: specification or target "
                "outcome is missing."
            )

        bucket = _bucket_timedelta(effective_spec.bucket)
        bucket_seconds = bucket.total_seconds()
        now = datetime.now(UTC)
        window_end = datetime.fromtimestamp(
            int(now.timestamp() // bucket_seconds) * bucket_seconds,
            tz=UTC,
        )
        window_start = window_end - effective_spec.lookback
        cache_payload = {
            "v": 3,
            "tenant_id": self._tenant_id,
            "target": effective_spec.target,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "bucket_seconds": bucket_seconds,
            "target_outcome": effective_outcome,
            "analysis_window": effective_window,
            "k_min": effective_spec.k_min,
            "k_max": effective_spec.k_max,
            "max_vertices": effective_spec.max_vertices,
        }
        cache_key = _make_cache_key(cache_payload)
        cached = await _cache_get(self._pg, self._tenant_id, cache_key)
        if cached and cached.get("status") == "success":
            return {
                "status": "skipped",
                "reason": "cache_hit",
                "cache_key": cache_key,
            }

        entity_id = _parse_entity_target(effective_spec.target)
        if not entity_id:
            return await self._skip(
                effective_spec,
                cache_key,
                window_start,
                window_end,
                "unsupported_target",
            )

        relationship_types = list(_ALLOWED_ESKG_RELATIONSHIPS)
        if effective_spec.include_presence_links:
            relationship_types.extend(_PRESENCE_PIVOT_RELATIONSHIPS)
        neighborhood = await self._graph.get_entity_k_hop_neighbors(
            entity_id=entity_id,
            k_min=effective_spec.k_min,
            k_max=effective_spec.k_max,
            max_vertices=effective_spec.max_vertices,
            relationship_types=relationship_types,
            tenant_id=self._tenant_id,
        )
        entity_scope = tuple(dict.fromkeys((entity_id, *neighborhood)))
        event_ids = await self._graph.get_event_ids_for_entities(
            entity_ids=list(entity_scope),
            window_start=window_start,
            window_end=window_end,
            max_events=effective_spec.max_events,
            relationship_types=_PRESENCE_PIVOT_RELATIONSHIPS,
            tenant_id=self._tenant_id,
        )
        vertex_scope = tuple(dict.fromkeys((*entity_scope, *event_ids)))

        (
            state_rows,
            embedding_rows,
            event_rows,
            relationship_rows,
        ) = await asyncio.gather(
            _load_state_rows(
                self._pg,
                tenant_id=self._tenant_id,
                entity_ids=entity_scope,
                window_start=window_start,
                window_end=window_end,
                max_rows=effective_spec.max_states,
            ),
            _load_embedding_rows(
                self._pg,
                tenant_id=self._tenant_id,
                entity_ids=entity_scope,
                window_start=window_start,
                window_end=window_end,
                max_rows=effective_spec.max_states,
            ),
            _load_event_rows(
                self._pg,
                tenant_id=self._tenant_id,
                event_ids=event_ids,
                window_start=window_start,
                window_end=window_end,
                max_rows=effective_spec.max_events,
            ),
            self._graph.get_relationship_observations(
                vertex_ids=list(vertex_scope),
                relationship_types=tuple(relationship_types),
                tenant_id=self._tenant_id,
            ),
        )
        observation_window = _build_observation_window(
            window_start=window_start,
            window_end=window_end,
            bucket_seconds=bucket.total_seconds(),
            state_rows=state_rows,
            embedding_rows=embedding_rows,
            relationship_rows=relationship_rows,
            event_rows=event_rows,
        )
        if not observation_window.observations:
            return await self._skip(
                effective_spec,
                cache_key,
                window_start,
                window_end,
                "empty_slice",
            )

        router = AmarthRouter(strict_dag=True)
        try:
            result = await asyncio.to_thread(
                router.analyze_observation_window,
                observation_window,
                effective_outcome,
                analysis_window_size=effective_window,
            )
        except (ValueError, TypeError, RuntimeError) as exc:
            await _cache_put(
                self._pg,
                tenant_id=self._tenant_id,
                cache_key=cache_key,
                target=effective_spec.target,
                window_start=window_start,
                window_end=window_end,
                status="failed",
                result_summary={"error": str(exc)},
            )
            raise CausalJobError(str(exc)) from exc

        links = tuple(result.get("causal_links", ()))
        effects = tuple(result.get("causal_effects", ()))
        effects_by_edge = _effect_index(effects)
        persisted = 0
        for link in links:
            if not isinstance(link, CausalLink):
                continue
            properties = self._causal_properties(
                link=link,
                effect=effects_by_edge.get(
                    (link.source_feature, link.target_feature)
                ),
                inference_id=cache_key,
                window_start=window_start,
                window_end=window_end,
                bucket=bucket,
                target=effective_spec.target,
                samples_processed=int(
                    result.get("metadata", {}).get("samples_processed", 0)
                ),
            )
            await self._graph.upsert_causal_link(
                source_feature=link.source_feature,
                target_feature=link.target_feature,
                properties=properties,
                tenant_id=self._tenant_id,
            )
            persisted += 1

        summary = {
            "persisted_edges": persisted,
            "causal_links": len(links),
            "validated_effects": len(effects),
            "entity_scope_size": len(entity_scope),
            "event_scope_size": len(event_ids),
            "observation_count": len(observation_window.observations),
            "relationship_count": len(observation_window.relationships),
            "counterfactual_ready": bool(
                result.get("metadata", {}).get("counterfactual_ready", False)
            ),
        }
        await _cache_put(
            self._pg,
            tenant_id=self._tenant_id,
            cache_key=cache_key,
            target=effective_spec.target,
            window_start=window_start,
            window_end=window_end,
            status="success",
            result_summary=summary,
        )
        return {"status": "success", "cache_key": cache_key, **summary}

    async def _skip(
        self,
        spec: CausalSliceSpec,
        cache_key: str,
        window_start: datetime,
        window_end: datetime,
        reason: str,
    ) -> dict[str, Any]:
        await _cache_put(
            self._pg,
            tenant_id=self._tenant_id,
            cache_key=cache_key,
            target=spec.target,
            window_start=window_start,
            window_end=window_end,
            status="skipped",
            result_summary={"reason": reason},
        )
        return {"status": "skipped", "reason": reason, "cache_key": cache_key}

    @staticmethod
    def _causal_properties(
        *,
        link: CausalLink,
        effect: object | None,
        inference_id: str,
        window_start: datetime,
        window_end: datetime,
        bucket: timedelta,
        target: str,
        samples_processed: int,
    ) -> dict[str, Any]:
        """Serializes statistical evidence and replay metadata onto CAUSES."""
        properties: dict[str, Any] = {
            "inference_id": inference_id,
            "confidence_score": link.confidence_score,
            "time_lag_seconds": link.time_lag_seconds,
            "lag_steps": link.lag_steps,
            "effect_size": link.effect_size,
            "p_value": link.p_value,
            "q_value": link.q_value,
            "stability": link.stability,
            "method": link.method,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "bucket_seconds": bucket.total_seconds(),
            "target": target,
            "samples_processed": samples_processed,
            "source_node_ids": list(link.source_node_ids),
            "target_node_ids": list(link.target_node_ids),
            "supports_counterfactual": link.supports_counterfactual,
            "counterfactual_model": "dowhy.gcm.StructuralCausalModel",
            "updated_at": window_end.isoformat(),
        }
        ate = getattr(effect, "ate", None)
        if isinstance(ate, (int, float)):
            properties["average_treatment_effect"] = float(ate)
            properties["refutation_passed"] = bool(
                getattr(effect, "refutation_passed", False)
            )
        return properties


def build_slice_spec_from_step_params(params: Any) -> CausalSliceSpec:
    """Builds a bounded slice while retaining version-one configuration fields."""
    lookback = _parse_lookback(getattr(params, "lookback", None))
    bucket = str(getattr(params, "bucket", None) or "1h")
    max_events = max(1, int(getattr(params, "max_events", None) or 20_000))
    max_states = max(1, int(getattr(params, "max_states", None) or 20_000))
    target = str(getattr(params, "target", None) or "entity:")
    k_min = max(1, int(getattr(params, "k_min", None) or 1))
    k_max = max(k_min, int(getattr(params, "k_max", None) or 2))
    max_vertices = max(1, int(getattr(params, "max_vertices", None) or 500))
    include_presence_links = bool(
        getattr(params, "include_presence_links", True)
    )

    mappings = getattr(params, "state_metrics", None)
    parsed: list[MetricMapping] = []
    if isinstance(mappings, list):
        for mapping in mappings:
            if not isinstance(mapping, dict):
                continue
            metric_id = mapping.get("metric_id")
            expr_sql = mapping.get("expr_sql")
            agg_sql = mapping.get("agg_sql")
            if all(
                isinstance(value, str)
                for value in (metric_id, expr_sql, agg_sql)
            ):
                parsed.append(
                    MetricMapping(
                        metric_id=str(metric_id),
                        expr_sql=str(expr_sql),
                        agg_sql=str(agg_sql),
                    )
                )

    return CausalSliceSpec(
        target=target,
        lookback=lookback,
        bucket=bucket,
        max_events=max_events,
        max_states=max_states,
        state_metrics=tuple(parsed) or _default_state_metrics_v1(),
        k_min=k_min,
        k_max=k_max,
        max_vertices=max_vertices,
        include_presence_links=include_presence_links,
    )
