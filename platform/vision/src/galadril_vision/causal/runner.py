from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, cast, LiteralString

import hashlib
import orjson
import pandas as pd
import structlog
from psycopg import sql

from amarth.router import AmarthRouter

from galadril_vision.common.exceptions import GaladrilVisionError
from galadril_vision.connectors.postgres.client import PostgresClient
from galadril_vision.connectors.postgres.graph import GraphStore

logger = structlog.get_logger(__name__)


class CausalJobError(GaladrilVisionError):
    """Raised when a causal job fails."""


@dataclass(frozen=True, slots=True)
class MetricMapping:
    metric_id: str
    expr_sql: str
    agg_sql: str


@dataclass(frozen=True, slots=True)
class CausalSliceSpec:
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
)

_PRESENCE_PIVOT_RELATIONSHIPS: tuple[str, ...] = (
    "APPEARS_IN",
    "PARTICIPATED_IN",
)


def _parse_lookback(value: Optional[str]) -> timedelta:
    if not value:
        return timedelta(days=7)

    v = value.strip().lower()
    if v.endswith("d"):
        return timedelta(days=int(v[:-1]))
    if v.endswith("h"):
        return timedelta(hours=int(v[:-1]))
    if v.endswith("m"):
        return timedelta(minutes=int(v[:-1]))
    raise ValueError(f"Unsupported lookback format: '{value}'")


def _normalize_bucket(value: Optional[str]) -> str:
    if not value:
        return "1 hour"
    v = value.strip().lower()
    if v.endswith("m"):
        return f"{int(v[:-1])} minutes"
    if v.endswith("h"):
        return f"{int(v[:-1])} hours"
    if v.endswith("d"):
        return f"{int(v[:-1])} days"
    return value


def _make_cache_key(payload: dict[str, Any]) -> str:
    raw = orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(raw).hexdigest()


def _parse_entity_target(target: str) -> Optional[str]:
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


def _build_state_metrics_query(state_metrics: tuple[MetricMapping, ...]) -> str:
    selects: list[str] = []
    for m in state_metrics:
        selects.append(f"{m.agg_sql}({m.expr_sql}) AS {m.metric_id}")
    select_sql = ",\n                ".join(selects)
    return f"""
            SELECT
                time_bucket($1::interval, event_time) AS ts,
                state_type,
                {select_sql}
            FROM entity_states
            WHERE event_time >= $2 AND event_time <= $3
            GROUP BY ts, state_type
            ORDER BY ts ASC
            LIMIT $4
    """


async def _cache_get(
    client: PostgresClient, cache_key: str
) -> Optional[dict[str, Any]]:
    async with client.connection() as conn:
        result = await conn.execute(
            """
            SELECT cache_key, status, result_summary
            FROM causal_runs
            WHERE cache_key = $1
            """,
            (cache_key,),
        )
        row = await result.fetchone()
        if not row:
            return None
        summary = row[2]
        if isinstance(summary, str):
            try:
                summary = orjson.loads(summary)
            except Exception:
                summary = {}
        return {
            "cache_key": row[0],
            "status": row[1],
            "result_summary": summary,
        }


async def _cache_put(
    client: PostgresClient,
    *,
    cache_key: str,
    target: str,
    window_start: datetime,
    window_end: datetime,
    status: str,
    result_summary: dict[str, Any],
) -> None:
    async with client.connection() as conn:
        await conn.execute(
            """
            INSERT INTO causal_runs (cache_key, target, window_start, window_end, status, result_summary)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            ON CONFLICT (cache_key) DO UPDATE SET
                created_at = NOW(),
                status = EXCLUDED.status,
                result_summary = EXCLUDED.result_summary,
                window_start = EXCLUDED.window_start,
                window_end = EXCLUDED.window_end,
                target = EXCLUDED.target
            """,
            (
                cache_key,
                target,
                window_start,
                window_end,
                status,
                orjson.dumps(result_summary).decode(),
            ),
        )


async def _load_event_intensity_frame_scoped(
    client: PostgresClient,
    *,
    window_start: datetime,
    window_end: datetime,
    bucket: str,
    max_rows: int,
    event_ids: list[str],
) -> pd.DataFrame:
    if not event_ids:
        return pd.DataFrame(columns=["timestamp"])

    async with client.connection() as conn:
        result = await conn.execute(
            """
            SELECT
                time_bucket($1::interval, event_time) AS ts,
                event_type,
                COUNT(*)::double precision AS intensity
            FROM eskg_events
            WHERE event_time >= $2 AND event_time <= $3
              AND event_id = ANY($5::text[])
            GROUP BY ts, event_type
            ORDER BY ts ASC
            LIMIT $4
            """,
            (bucket, window_start, window_end, max_rows, event_ids),
        )
        rows = await result.fetchall()

    if not rows:
        return pd.DataFrame(columns=["timestamp"])

    df = pd.DataFrame(rows, columns=["timestamp", "event_type", "intensity"])
    pivot = df.pivot_table(
        index="timestamp",
        columns="event_type",
        values="intensity",
        aggfunc="sum",
        fill_value=0.0,
    )
    pivot.columns = [f"event_intensity.{c}" for c in pivot.columns]
    return pivot.reset_index()


async def _load_state_metrics_frame_scoped(
    client: PostgresClient,
    *,
    window_start: datetime,
    window_end: datetime,
    bucket: str,
    max_rows: int,
    state_metrics: tuple[MetricMapping, ...],
    entity_ids: list[str],
) -> pd.DataFrame:
    if not entity_ids:
        return pd.DataFrame(columns=["timestamp"])

    raw_query_string = _build_state_metrics_query(state_metrics).replace(
        "WHERE event_time >= $2 AND event_time <= $3",
        "WHERE event_time >= $2 AND event_time <= $3 AND entity_id = ANY($5::text[])",
    )
    safe_query_string = cast(LiteralString, raw_query_string)
    query = sql.SQL(safe_query_string)

    async with client.connection() as conn:
        result = await conn.execute(
            query,
            (bucket, window_start, window_end, max_rows, entity_ids),
        )
        rows = await result.fetchall()

    if not rows:
        return pd.DataFrame(columns=["timestamp"])

    cols = ["timestamp", "state_type"] + [m.metric_id for m in state_metrics]
    df = pd.DataFrame(rows, columns=cols)

    frames: list[pd.DataFrame] = []
    for m in state_metrics:
        pivot = df.pivot_table(
            index="timestamp",
            columns="state_type",
            values=m.metric_id,
            aggfunc="mean",
            fill_value=0.0,
        )
        pivot.columns = [f"{m.metric_id}.{c}" for c in pivot.columns]
        frames.append(pivot)

    out = pd.concat(frames, axis=1).reset_index()
    return out.fillna(0.0)


def _merge_frames(
    event_df: pd.DataFrame, state_df: pd.DataFrame
) -> pd.DataFrame:
    if event_df.empty and state_df.empty:
        return pd.DataFrame()

    if event_df.empty:
        return state_df

    if state_df.empty:
        return event_df

    df = pd.merge(event_df, state_df, how="outer", on="timestamp")
    return df.sort_values("timestamp").fillna(0.0).reset_index(drop=True)


def _effect_p_value(effect: Any) -> tuple[float | None, str]:
    p_val = getattr(effect, "p_value", None)
    if isinstance(p_val, (float, int)):
        return float(p_val), "direct"
    return None, "unavailable"


class AmarthCausalRunner:
    def __init__(self, pg: PostgresClient, graph: GraphStore) -> None:
        self._pg = pg
        self._graph = graph

    async def run(
        self,
        *,
        spec: CausalSliceSpec,
        target_outcome: str,
        window_size: Optional[str],
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        window_start = now - spec.lookback
        bucket = _normalize_bucket(spec.bucket)

        cache_payload = {
            "v": 2,
            "target": spec.target,
            "window_start": window_start.isoformat(),
            "window_end": now.isoformat(),
            "bucket": bucket,
            "max_events": spec.max_events,
            "max_states": spec.max_states,
            "target_outcome": target_outcome,
            "window_size": window_size,
            "state_metrics": [m.metric_id for m in spec.state_metrics],
            "k_min": spec.k_min,
            "k_max": spec.k_max,
            "max_vertices": spec.max_vertices,
            "include_presence_links": spec.include_presence_links,
        }
        cache_key = _make_cache_key(cache_payload)

        cached = await _cache_get(self._pg, cache_key)
        if cached and cached.get("status") == "success":
            logger.info(
                "causal_cache_hit", cache_key=cache_key, target=spec.target
            )
            return {
                "status": "skipped",
                "reason": "cache_hit",
                "cache_key": cache_key,
            }

        entity_id = _parse_entity_target(spec.target)
        if not entity_id:
            await _cache_put(
                self._pg,
                cache_key=cache_key,
                target=spec.target,
                window_start=window_start,
                window_end=now,
                status="skipped",
                result_summary={"reason": "unsupported_target"},
            )
            return {
                "status": "skipped",
                "reason": "unsupported_target",
                "cache_key": cache_key,
            }

        rel_types = list(_ALLOWED_ESKG_RELATIONSHIPS)
        if spec.include_presence_links:
            rel_types.extend(_PRESENCE_PIVOT_RELATIONSHIPS)

        neighborhood = await self._graph.get_entity_k_hop_neighbors(
            entity_id=entity_id,
            k_min=spec.k_min,
            k_max=spec.k_max,
            max_vertices=spec.max_vertices,
            relationship_types=rel_types,
        )
        entity_scope = [entity_id] + [
            eid for eid in neighborhood if eid != entity_id
        ]

        event_ids = await self._graph.get_event_ids_for_entities(
            entity_ids=entity_scope,
            window_start=window_start,
            window_end=now,
            max_events=spec.max_events,
            relationship_types=_PRESENCE_PIVOT_RELATIONSHIPS,
        )

        events_df = await _load_event_intensity_frame_scoped(
            self._pg,
            window_start=window_start,
            window_end=now,
            bucket=bucket,
            max_rows=spec.max_events,
            event_ids=event_ids,
        )
        states_df = await _load_state_metrics_frame_scoped(
            self._pg,
            window_start=window_start,
            window_end=now,
            bucket=bucket,
            max_rows=spec.max_states,
            state_metrics=spec.state_metrics,
            entity_ids=entity_scope,
        )

        df = _merge_frames(events_df, states_df)
        if df.empty:
            await _cache_put(
                self._pg,
                cache_key=cache_key,
                target=spec.target,
                window_start=window_start,
                window_end=now,
                status="skipped",
                result_summary={"reason": "empty_slice"},
            )
            return {
                "status": "skipped",
                "reason": "empty_slice",
                "cache_key": cache_key,
            }

        if target_outcome not in df.columns:
            await _cache_put(
                self._pg,
                cache_key=cache_key,
                target=spec.target,
                window_start=window_start,
                window_end=now,
                status="skipped",
                result_summary={
                    "reason": "missing_target_outcome",
                    "target_outcome": target_outcome,
                },
            )
            return {
                "status": "skipped",
                "reason": "missing_target_outcome",
                "cache_key": cache_key,
            }

        router = AmarthRouter(strict_dag=True)
        try:
            result = router.analyze(
                df=df,
                target_outcome=target_outcome,
                time_col="timestamp",
                embedding_col=None,
                prior_graph=None,
                window_size=window_size,
            )
        except Exception as exc:
            await _cache_put(
                self._pg,
                cache_key=cache_key,
                target=spec.target,
                window_start=window_start,
                window_end=now,
                status="failed",
                result_summary={"error": str(exc)},
            )
            raise CausalJobError(str(exc)) from exc

        effects = result.get("causal_effects", []) or []
        persisted = 0

        for eff in effects:
            treatment = getattr(eff, "treatment", None)
            outcome = getattr(eff, "outcome", None)
            ate = getattr(eff, "ate", None)
            refutation_passed = getattr(eff, "refutation_passed", None)

            if not treatment or not outcome:
                continue
            if not isinstance(ate, (float, int)):
                continue
            if refutation_passed is False:
                continue

            p_value, p_value_source = _effect_p_value(eff)

            props: dict[str, Any] = {
                "causal_validated": True,
                "ate": float(ate),
                "p_value": p_value,
                "p_value_source": p_value_source,
                "method": getattr(eff, "method_name", "unknown"),
                "refutation_passed": bool(refutation_passed)
                if refutation_passed is not None
                else True,
                "window_start": window_start.isoformat(),
                "window_end": now.isoformat(),
                "cache_key": cache_key,
                "samples_processed": int(
                    result.get("metadata", {}).get("samples_processed", len(df))
                ),
                "bucket": bucket,
                "target": spec.target,
                "k_min": spec.k_min,
                "k_max": spec.k_max,
                "updated_at": now.isoformat(),
            }

            if getattr(eff, "stderr", None) is not None:
                props["stderr"] = getattr(eff, "stderr")
            if getattr(eff, "ci_lower", None) is not None:
                props["ci_lower"] = getattr(eff, "ci_lower")
            if getattr(eff, "ci_upper", None) is not None:
                props["ci_upper"] = getattr(eff, "ci_upper")

            await self._graph.upsert_metric_influence(
                source_metric=str(treatment),
                target_metric=str(outcome),
                properties=props,
            )
            persisted += 1

        await _cache_put(
            self._pg,
            cache_key=cache_key,
            target=spec.target,
            window_start=window_start,
            window_end=now,
            status="success",
            result_summary={
                "persisted_edges": persisted,
                "effects": len(effects),
                "entity_scope_size": len(entity_scope),
                "event_scope_size": len(event_ids),
            },
        )

        return {
            "status": "success",
            "cache_key": cache_key,
            "persisted_edges": persisted,
            "effects": len(effects),
        }


def build_slice_spec_from_step_params(params: Any) -> CausalSliceSpec:
    lookback = _parse_lookback(getattr(params, "lookback", None))
    bucket = getattr(params, "bucket", None) or "1h"
    max_events = int(getattr(params, "max_events", None) or 20000)
    max_states = int(getattr(params, "max_states", None) or 20000)
    target = str(getattr(params, "target", None) or "entity:")

    k_min = int(getattr(params, "k_min", None) or 1)
    k_max = int(getattr(params, "k_max", None) or 2)
    if k_min < 1:
        k_min = 1
    if k_max < k_min:
        k_max = k_min

    max_vertices = int(getattr(params, "max_vertices", None) or 500)
    include_presence_links = bool(
        getattr(params, "include_presence_links", True)
    )

    mappings = getattr(params, "state_metrics", None)
    if isinstance(mappings, list) and mappings:
        parsed: list[MetricMapping] = []
        for m in mappings:
            if not isinstance(m, dict):
                continue
            metric_id = m.get("metric_id")
            expr_sql = m.get("expr_sql")
            agg_sql = m.get("agg_sql")
            if (
                isinstance(metric_id, str)
                and isinstance(expr_sql, str)
                and isinstance(agg_sql, str)
            ):
                parsed.append(
                    MetricMapping(
                        metric_id=metric_id, expr_sql=expr_sql, agg_sql=agg_sql
                    )
                )
        state_metrics = tuple(parsed) if parsed else _default_state_metrics_v1()
    else:
        state_metrics = _default_state_metrics_v1()

    return CausalSliceSpec(
        target=target,
        lookback=lookback,
        bucket=str(bucket),
        max_events=max_events,
        max_states=max_states,
        state_metrics=state_metrics,
        k_min=k_min,
        k_max=k_max,
        max_vertices=max_vertices,
        include_presence_links=include_presence_links,
    )
