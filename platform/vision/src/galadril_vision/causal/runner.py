"""Vision adapter for time-windowed Amarth inference over ESKG observations."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import orjson
import structlog
from amarth.observations import (
    CausalLink,
    GraphRelationshipObservation,
    Observation,
    ObservationWindow,
)
from amarth.router import AmarthRouter
from galadril_ontology import (
    GolDenied,
    GolEffect,
    active_gol_lease,
    make_gol_state_effect,
    protected_gol_effects,
    protected_gol_state_effect,
    scoped_instance_id,
)
from galadril_vision.common.exceptions import GaladrilVisionError
from galadril_vision.common.types import normalize_tenant_id
from galadril_vision.connectors.postgres.client import PostgresClient
from galadril_vision.connectors.postgres.graph import GraphStore
from pydantic import JsonValue, TypeAdapter, ValidationError

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_STATE_SCOPE = (
    "tenant_id",
    "entity_id",
    "event_id",
    "state_type",
)
_STATE_FEATURES = ("event_time", "confidence", "scalar_names", "scalar_values")
_ESTIMATE_COLUMNS = (
    ("effect_sizes", "effect_size"),
    ("confidence_scores", "confidence_score"),
    ("lag_seconds", "time_lag_seconds"),
    ("p_values", "p_value"),
    ("q_values", "q_value"),
    ("stabilities", "stability"),
    ("counterfactual_flags", "supports_counterfactual"),
)

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


def _make_cache_key(payload: Mapping[str, object]) -> str:
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
    client: PostgresClient, tenant_id: str, cache_key: str, target: str
) -> dict[str, object] | None:
    if active_gol_lease() is None:
        raise GolDenied("Causal cache requires an admitted generation")
    async with client.tenant_connection(tenant_id) as conn:
        result = await conn.execute(
            """
            SELECT result_summary
            FROM causal_runs
            WHERE tenant_id = %s AND cache_key = %s AND target = %s
            """,
            (tenant_id, cache_key, target),
        )
        row = await result.fetchone()
    if row is None:
        return None
    (metadata,) = row
    if not isinstance(metadata, dict):
        raise GolDenied("Causal cache unavailable")
    effect = _read_evidence(metadata, fields=("cache_key", "target", "status"))
    (signed,) = await _admitted_evidence((effect,), tenant_id)
    if signed.get("cache_key") != cache_key or signed.get("target") != target:
        raise GolDenied("Causal cache scope changed")
    return signed


def _causal_run_value(summary: Mapping[str, object]) -> dict[str, JsonValue]:
    """Keeps every result column individually governable by GOL."""
    typed = _JSON_OBJECT.validate_python(dict(summary))
    estimates = typed.pop("root_estimates", [])
    if not isinstance(estimates, list) or not all(
        isinstance(row, dict) for row in estimates
    ):
        raise CausalJobError("Causal estimates unavailable")
    for column, attribute in _ESTIMATE_COLUMNS:
        typed[column] = [
            row[attribute] for row in estimates if isinstance(row, dict)
        ]
    for column in (
        "persisted_edges",
        "causal_links",
        "validated_effects",
        "entity_scope_size",
        "event_scope_size",
        "observation_count",
        "relationship_count",
    ):
        if column in typed:
            typed[column] = str(typed[column])
    return typed


async def _cache_put(
    client: PostgresClient,
    *,
    tenant_id: str,
    cache_key: str,
    target: str,
    window_start: datetime,
    window_end: datetime,
    status: str,
    result_summary: Mapping[str, object],
) -> None:
    value = {
        **_causal_run_value(result_summary),
        "cache_key": cache_key,
        "target": target,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "status": status,
    }
    effect = make_gol_state_effect(
        operation="write",
        tenant_id=tenant_id,
        destination="causal_state",
        purpose="causal_analysis",
        instance_id=scoped_instance_id("causal-cache:" + cache_key),
        proposal=value,
    )
    async with protected_gol_effects((effect,), tenant_id=tenant_id) as (
        receipt,
    ):
        if not receipt:
            raise GolDenied("Causal cache receipt unavailable")
        async with client.tenant_connection(tenant_id) as conn:
            await conn.execute(
                """
                INSERT INTO causal_runs (
                    tenant_id, cache_key, target, window_start, window_end,
                    status, result_summary
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
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
                    orjson.dumps(
                        {
                            "gol_value": value,
                            "gol_receipt": receipt,
                            "gol_symbol_key": effect.target,
                            "gol_id": effect.instance_id,
                        }
                    ).decode(),
                ),
            )


def _read_evidence(
    metadata: object,
    *,
    fields: Sequence[str],
    prefix: str = "gol",
) -> GolEffect:
    """Builds a read request from the signed proposal and its stored scope."""
    if not isinstance(metadata, dict):
        raise GolDenied("Causal evidence unavailable")
    try:
        value = _JSON_OBJECT.validate_python(
            metadata.get(f"{prefix}_value"), strict=True
        )
        effect = GolEffect.model_validate(
            {
                "operation": "read",
                "target": metadata.get(f"{prefix}_symbol_key"),
                "destination": "causal_state",
                "purpose": "causal_analysis",
                "instanceId": metadata.get(f"{prefix}_id"),
                "receipt": metadata.get(f"{prefix}_receipt"),
                "proposal": value,
                "readFields": tuple(
                    field for field in fields if field in value
                ),
            }
        )
    except ValidationError:
        raise GolDenied("Causal evidence unavailable") from None
    if not effect.instance_id or not effect.receipt:
        raise GolDenied("Causal evidence unavailable")
    return effect


async def _admitted_evidence(
    effects: Iterable[GolEffect], tenant_id: str
) -> list[dict[str, JsonValue]]:
    """Keeps every projected value inside the corresponding live permit."""
    batch: list[GolEffect] = []
    values: list[dict[str, JsonValue]] = []
    size = 0
    for effect in effects:
        encoded_size = len(
            orjson.dumps(
                effect.model_dump(mode="json", by_alias=True, exclude_none=True)
            )
        )
        if batch and (len(batch) == 64 or size + encoded_size > 144 * 1024):
            async with protected_gol_effects(batch, tenant_id=tenant_id):
                values.extend(map(_project_evidence, batch))
            batch.clear()
            size = 0
        batch.append(effect)
        size += encoded_size + 1
    if batch:
        async with protected_gol_effects(batch, tenant_id=tenant_id):
            values.extend(map(_project_evidence, batch))
    return values


def _project_evidence(effect: GolEffect) -> dict[str, JsonValue]:
    if effect.proposal is None or effect.read_fields is None:
        raise GolDenied("Causal evidence unavailable")
    return {field: effect.proposal[field] for field in effect.read_fields}


async def _load_state_rows(
    client: PostgresClient,
    *,
    tenant_id: str,
    entity_ids: Sequence[str],
    window_start: datetime,
    window_end: datetime,
    max_rows: int,
) -> tuple[tuple[datetime, str, Mapping[str, object], str, str], ...]:
    if active_gol_lease() is None:
        raise GolDenied("Causal evidence requires an admitted generation")
    if not entity_ids:
        return ()
    async with client.tenant_connection(tenant_id) as conn:
        result = await conn.execute(
            """
            SELECT event_time, state_type, state_value, entity_id, event_id
            FROM entity_states
            WHERE tenant_id = %s AND event_time >= %s AND event_time <= %s
              AND entity_id = ANY(%s::text[])
            ORDER BY event_time, entity_id, event_id, state_type
            LIMIT %s
            """,
            (
                tenant_id,
                window_start,
                window_end,
                list(entity_ids),
                max_rows + 1,
            ),
        )
        rows = await result.fetchall()
    if len(rows) > max_rows:
        raise GolDenied("Causal evidence exceeds the collection budget")
    effects = (
        _read_evidence(
            metadata, fields=_STATE_SCOPE + _STATE_FEATURES, prefix="gol_state"
        )
        for _, _, metadata, _, _ in rows
    )
    output: list[tuple[datetime, str, Mapping[str, object], str, str]] = []
    values = await _admitted_evidence(effects, tenant_id)
    for (created_at, state_type, _, entity_id, event_id), value in zip(
        rows, values, strict=True
    ):
        keys = (tenant_id, entity_id, event_id, state_type)
        if any(
            value.get(field) != expected
            for field, expected in zip(_STATE_SCOPE, keys, strict=True)
        ):
            raise GolDenied("Causal observation scope changed")
        observed_at = value.get("event_time")
        try:
            if (
                not isinstance(observed_at, str)
                or datetime.fromisoformat(observed_at) != created_at
            ):
                raise GolDenied("Causal observation time changed")
        except ValueError:
            raise GolDenied("Causal observation time unavailable") from None
        numeric: dict[str, object] = {}
        if "confidence" in value:
            numeric["confidence"] = value["confidence"]
        names, scalars = value.get("scalar_names"), value.get("scalar_values")
        if names is not None or scalars is not None:
            if (
                not isinstance(names, list)
                or not isinstance(scalars, list)
                or len(names) != len(scalars)
                or not all(isinstance(name, str) for name in names)
            ):
                raise GolDenied("Causal scalar observation unavailable")
            numeric["scalar_evidence"] = dict(zip(names, scalars, strict=True))
        output.append((created_at, state_type, numeric, entity_id, event_id))
    return tuple(output)


async def _load_embedding_rows(
    client: PostgresClient,
    *,
    tenant_id: str,
    entity_ids: Sequence[str],
    window_start: datetime,
    window_end: datetime,
    max_rows: int,
) -> tuple[tuple[datetime, str, object, str, Mapping[str, object], str], ...]:
    async with protected_gol_state_effect(
        operation="read",
        tenant_id=tenant_id,
        destination="causal_state",
        purpose="causal_analysis",
    ):
        if not entity_ids:
            return ()
        async with client.tenant_connection(tenant_id) as conn:
            result = await conn.execute(
                """
                SELECT created_at, modality, embedding, entity_id, metadata, id
                FROM entity_embeddings
                WHERE created_at >= %s AND created_at <= %s
                  AND entity_id = ANY(%s::text[])
                ORDER BY created_at ASC
                LIMIT %s
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
    async with protected_gol_state_effect(
        operation="read",
        tenant_id=tenant_id,
        destination="causal_state",
        purpose="causal_analysis",
    ):
        if not event_ids:
            return ()
        async with client.tenant_connection(tenant_id) as conn:
            result = await conn.execute(
                """
                SELECT event_time, event_type, properties, event_id
                FROM eskg_events
                WHERE event_time >= %s AND event_time <= %s
                  AND event_id = ANY(%s::text[])
                ORDER BY event_time ASC
                LIMIT %s
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
    numeric = {
        key: float(value)
        for key, value in values.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    evidence = values.get("scalar_evidence")
    if isinstance(evidence, Mapping):
        numeric.update(
            {
                key: float(value)
                for key, value in evidence.items()
                if isinstance(key, str)
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            }
        )
    return numeric


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


def _root_estimates(
    entity_id: str, links: Sequence[object]
) -> list[dict[str, JsonValue]]:
    """Publishes root statistics without exposing source identities or paths."""
    return [
        {
            "effect_size": link.effect_size,
            "confidence_score": link.confidence_score,
            "time_lag_seconds": link.time_lag_seconds,
            "p_value": link.p_value,
            "q_value": link.q_value,
            "stability": link.stability,
            "supports_counterfactual": link.supports_counterfactual,
        }
        for link in links
        if isinstance(link, CausalLink) and entity_id in link.target_node_ids
    ][:256]


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
    ) -> dict[str, object]:
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
        lease = active_gol_lease()
        if lease is None:
            raise GolDenied("Causal analysis requires an admitted generation")
        cache_payload = {
            "v": 6,
            "gol_generation": lease.generation_id,
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
            "max_states": effective_spec.max_states,
            "max_events": effective_spec.max_events,
            "include_presence_links": effective_spec.include_presence_links,
        }
        cache_key = _make_cache_key(cache_payload)
        cached = await _cache_get(
            self._pg, self._tenant_id, cache_key, effective_spec.target
        )
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

        if effective_spec.k_max == 0:
            entity_scope = (entity_id,)
            state_rows = await _load_state_rows(
                self._pg,
                tenant_id=self._tenant_id,
                entity_ids=entity_scope,
                window_start=window_start,
                window_end=window_end,
                max_rows=effective_spec.max_states,
            )
            event_ids = tuple(dict.fromkeys(row[4] for row in state_rows))
            if len(event_ids) > effective_spec.max_events:
                raise GolDenied(
                    "Causal event scope exceeds the collection budget"
                )
            embedding_rows = ()
            event_rows = ()
            relationship_rows = ()
        else:
            (
                entity_scope,
                event_ids,
                state_rows,
                embedding_rows,
                event_rows,
                relationship_rows,
            ) = await self._graph_window(
                effective_spec, entity_id, window_start, window_end
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
        except (ValueError, TypeError, RuntimeError):
            await _cache_put(
                self._pg,
                tenant_id=self._tenant_id,
                cache_key=cache_key,
                target=effective_spec.target,
                window_start=window_start,
                window_end=window_end,
                status="failed",
                result_summary={"error": "causal_analysis_failed"},
            )
            raise CausalJobError("Causal analysis unavailable") from None

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

        summary: dict[str, object] = {
            "root_estimates": _root_estimates(entity_id, links),
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
        return {"status": "success", "cache_key": cache_key}

    async def _graph_window(
        self,
        spec: CausalSliceSpec,
        entity_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[
        tuple[str, ...],
        tuple[str, ...],
        tuple[tuple[datetime, str, Mapping[str, object], str, str], ...],
        tuple[
            tuple[datetime, str, object, str, Mapping[str, object], str], ...
        ],
        tuple[tuple[datetime, str, Mapping[str, object], str], ...],
        tuple[tuple[str, str, str, dict[str, JsonValue]], ...],
    ]:
        relationship_types = list(_ALLOWED_ESKG_RELATIONSHIPS)
        if spec.include_presence_links:
            relationship_types.extend(_PRESENCE_PIVOT_RELATIONSHIPS)
        neighborhood = await self._graph.get_entity_k_hop_neighbors(
            entity_id=entity_id,
            k_min=spec.k_min,
            k_max=spec.k_max,
            max_vertices=spec.max_vertices,
            relationship_types=relationship_types,
            tenant_id=self._tenant_id,
        )
        entity_scope = tuple(dict.fromkeys((entity_id, *neighborhood)))
        event_ids = tuple(
            await self._graph.get_event_ids_for_entities(
                entity_ids=list(entity_scope),
                window_start=window_start,
                window_end=window_end,
                max_events=spec.max_events,
                relationship_types=_PRESENCE_PIVOT_RELATIONSHIPS,
                tenant_id=self._tenant_id,
            )
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
                max_rows=spec.max_states,
            ),
            _load_embedding_rows(
                self._pg,
                tenant_id=self._tenant_id,
                entity_ids=entity_scope,
                window_start=window_start,
                window_end=window_end,
                max_rows=spec.max_states,
            ),
            _load_event_rows(
                self._pg,
                tenant_id=self._tenant_id,
                event_ids=event_ids,
                window_start=window_start,
                window_end=window_end,
                max_rows=spec.max_events,
            ),
            self._graph.get_relationship_observations(
                vertex_ids=list(vertex_scope),
                relationship_types=tuple(relationship_types),
                tenant_id=self._tenant_id,
            ),
        )
        return (
            entity_scope,
            event_ids,
            state_rows,
            embedding_rows,
            event_rows,
            relationship_rows,
        )

    async def _skip(
        self,
        spec: CausalSliceSpec,
        cache_key: str,
        window_start: datetime,
        window_end: datetime,
        reason: str,
    ) -> dict[str, object]:
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
    ) -> dict[str, JsonValue]:
        """Serializes statistical evidence and replay metadata onto CAUSES."""
        properties: dict[str, JsonValue] = {
            "inference_id": inference_id,
            "confidence_score": link.confidence_score,
            "time_lag_seconds": link.time_lag_seconds,
            "lag_steps": str(link.lag_steps),
            "effect_size": link.effect_size,
            "p_value": link.p_value,
            "q_value": link.q_value,
            "stability": link.stability,
            "method": link.method,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "bucket_seconds": bucket.total_seconds(),
            "target": target,
            "samples_processed": str(samples_processed),
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


def build_slice_spec_from_step_params(params: object) -> CausalSliceSpec:
    """Builds a bounded slice while retaining version-one configuration fields."""
    lookback = _parse_lookback(getattr(params, "lookback", None))
    bucket = str(getattr(params, "bucket", None) or "1h")
    max_events = max(1, int(getattr(params, "max_events", None) or 20_000))
    max_states = max(1, int(getattr(params, "max_states", None) or 20_000))
    target = str(getattr(params, "target", None) or "entity:")
    k_min_value = getattr(params, "k_min", None)
    k_max_value = getattr(params, "k_max", None)
    k_min = max(0, int(k_min_value) if k_min_value is not None else 1)
    k_max = max(k_min, int(k_max_value) if k_max_value is not None else 2)
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
