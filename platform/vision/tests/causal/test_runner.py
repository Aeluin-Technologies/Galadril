"""Tests for Vision's ESKG-to-Amarth observation bridge."""

import asyncio
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from typing import cast
from unittest.mock import patch

import pytest
from galadril_ontology import GolDenied, GolEffect
from galadril_vision.causal.runner import (
    _build_observation_window,
    _cache_get,
    _cache_put,
    _causal_run_value,
    _load_embedding_rows,
    _load_event_rows,
    _load_state_rows,
    _numeric_properties,
)
from galadril_vision.connectors.postgres.client import PostgresClient


class _Cursor:
    async def fetchone(self) -> None:
        return None

    async def fetchall(self) -> list[tuple[object, ...]]:
        return []


class _Connection:
    def __init__(self) -> None:
        self.statements = 0

    async def execute(
        self, query: str, parameters: tuple[object, ...]
    ) -> _Cursor:
        assert query.count("%s") == len(parameters)
        assert "$1" not in query
        self.statements += 1
        return _Cursor()


class _Client:
    def __init__(self) -> None:
        self.connection = _Connection()

    @asynccontextmanager
    async def tenant_connection(
        self, tenant_id: str
    ) -> AsyncIterator[_Connection]:
        assert tenant_id == "tenant-a"
        yield self.connection


def test_causal_queries_use_psycopg_placeholders() -> None:
    """Keeps every causal SQL path compatible with the live psycopg pool."""
    client = _Client()
    pg = cast(PostgresClient, client)
    now = datetime(2026, 9, 30, tzinfo=UTC)

    async def exercise() -> None:
        assert await _cache_get(pg, "tenant-a", "key", "entity:one") is None
        await _cache_put(
            pg,
            tenant_id="tenant-a",
            cache_key="key",
            target="entity:one",
            window_start=now,
            window_end=now,
            status="success",
            result_summary={"causal_links": 1},
        )
        assert (
            await _load_state_rows(
                pg,
                tenant_id="tenant-a",
                entity_ids=("one",),
                window_start=now,
                window_end=now,
                max_rows=30,
            )
            == ()
        )
        assert (
            await _load_embedding_rows(
                pg,
                tenant_id="tenant-a",
                entity_ids=("one",),
                window_start=now,
                window_end=now,
                max_rows=30,
            )
            == ()
        )
        assert (
            await _load_event_rows(
                pg,
                tenant_id="tenant-a",
                event_ids=("event-one",),
                window_start=now,
                window_end=now,
                max_rows=30,
            )
            == ()
        )

    asyncio.run(exercise())
    assert client.connection.statements == 5


def test_build_observation_window_keeps_states_embeddings_and_edges() -> None:
    """Builds one typed window without discarding multimodal graph evidence."""
    start = datetime(2026, 8, 24, tzinfo=UTC)
    window = _build_observation_window(
        window_start=start,
        window_end=start + timedelta(seconds=5),
        bucket_seconds=1.0,
        state_rows=(
            (
                start,
                "FacialExpressionShift",
                {"confidence": 0.91},
                "face-node",
                "face-event",
            ),
        ),
        embedding_rows=(
            (
                start,
                "face",
                [0.1, 0.2, 0.3],
                "face-node",
                {
                    "state_type": "FacialExpressionShift",
                    "event_id": "face-event",
                },
                "embedding-1",
            ),
        ),
        relationship_rows=(
            (
                "face-node",
                "text-node",
                "TRIGGERS",
                {"timestamp": (start + timedelta(seconds=2)).isoformat()},
            ),
        ),
    )

    assert len(window.observations) == 2
    assert window.observations[1].embeddings["face_embedding"] == (
        0.1,
        0.2,
        0.3,
    )
    assert window.relationships[0].relationship_type == "TRIGGERS"


def test_randomized_scalar_evidence_is_available_to_amarth() -> None:
    """Persists treatment and outcome as independent numerical features."""
    assert _numeric_properties(
        {
            "confidence": 0.99,
            "scalar_evidence": {"treatment": 1.0, "outcome": 6.5},
        }
    ) == {"confidence": 0.99, "treatment": 1.0, "outcome": 6.5}


@pytest.mark.anyio
@pytest.mark.parametrize("event", ["event-one", "wrong-event"])
async def test_causal_observation_uses_signed_values_only(event: str) -> None:
    now = datetime(2026, 9, 30, tzinfo=UTC)
    signed = {
        "tenant_id": "tenant-a",
        "entity_id": "one",
        "event_id": "event-one",
        "state_type": "Trial",
        "event_time": now.astimezone(timezone(timedelta(hours=2))).isoformat(),
        "confidence": 0.95,
        "scalar_names": ["signal"],
        "scalar_values": [1.5],
        "display_label": "private",
    }

    class Cursor(_Cursor):
        async def fetchall(self) -> list[tuple[object, ...]]:
            return [
                (
                    now,
                    "Trial",
                    {
                        "confidence": 100.0,
                        "gol_state_symbol_key": "test::ObservationState",
                        "gol_state_id": "state-one",
                        "gol_state_value": signed,
                        "gol_state_receipt": "signed-provenance",
                    },
                    "one",
                    event,
                )
            ]

    class Connection(_Connection):
        async def execute(
            self, query: str, parameters: tuple[object, ...]
        ) -> _Cursor:
            await super().execute(query, parameters)
            return Cursor()

    @asynccontextmanager
    async def authorize(
        effects: Sequence[GolEffect], *, tenant_id: str
    ) -> AsyncIterator[tuple[None]]:
        (effect,) = effects
        assert tenant_id == "tenant-a"
        assert effect.receipt == "signed-provenance"
        assert effect.instance_id == "state-one"
        assert effect.proposal == signed
        assert effect.read_fields is not None
        assert "display_label" not in effect.read_fields
        yield (None,)

    client = _Client()
    client.connection = Connection()

    async def read() -> tuple[tuple[datetime, str, object, str, str], ...]:
        return await _load_state_rows(
            cast(PostgresClient, client),
            tenant_id="tenant-a",
            entity_ids=("one",),
            window_start=now,
            window_end=now,
            max_rows=30,
        )

    with patch(
        "galadril_vision.causal.runner.protected_gol_effects",
        authorize,
        create=True,
    ):
        if event != "event-one":
            with pytest.raises(GolDenied):
                await read()
        else:
            assert await read() == (
                (
                    now,
                    "Trial",
                    {
                        "confidence": 0.95,
                        "scalar_evidence": {"signal": 1.5},
                    },
                    "one",
                    event,
                ),
            )


def test_root_estimates_do_not_publish_the_source_chain() -> None:
    from amarth.observations import CausalLink
    from galadril_vision.causal.runner import _root_estimates

    link = CausalLink(
        source_feature="private-source::signal",
        target_feature="root::outcome",
        confidence_score=0.9,
        time_lag_seconds=1.0,
        lag_steps=1,
        effect_size=2.0,
        stability=0.95,
        method="granger",
        source_node_ids=("private-source",),
        target_node_ids=("root",),
    )
    estimates = _root_estimates("root", (link,))
    assert len(estimates) == 1
    assert estimates[0]["effect_size"] == 2.0
    assert "private-source" not in str(estimates)
    assert _root_estimates("other", (link,)) == []


def test_causal_receipt_covers_statistics_without_opaque_nested_records() -> (
    None
):
    value = _causal_run_value(
        {
            "causal_links": 1,
            "root_estimates": [
                {
                    "effect_size": 2.0,
                    "confidence_score": 0.9,
                    "time_lag_seconds": 1.0,
                    "p_value": None,
                    "q_value": 0.05,
                    "stability": 0.95,
                    "supports_counterfactual": True,
                }
            ],
        }
    )
    assert value["causal_links"] == "1"
    assert value["effect_sizes"] == [2.0]
    assert value["p_values"] == [None]
    assert value["counterfactual_flags"] == [True]
    assert "root_estimates" not in value


def test_causal_cache_denial_performs_no_database_read_or_write() -> None:
    client = _Client()
    now = datetime(2026, 9, 30, tzinfo=UTC)

    async def denied() -> None:
        with pytest.raises(GolDenied):
            await _cache_get(
                cast(PostgresClient, client), "tenant-a", "cache", "entity:one"
            )
        with pytest.raises(GolDenied):
            await _cache_put(
                cast(PostgresClient, client),
                tenant_id="tenant-a",
                cache_key="cache",
                target="entity:one",
                window_start=now,
                window_end=now,
                status="success",
                result_summary={},
            )

    asyncio.run(denied())
    assert client.connection.statements == 0


@asynccontextmanager
async def _unit_scope(**_kwargs: object) -> AsyncIterator[str]:
    """Supplies a unit transport scope without claiming an authorization decision."""
    yield "unit-receipt"


@asynccontextmanager
async def _unit_effects(
    effects: Sequence[GolEffect], *, tenant_id: str | None = None
) -> AsyncIterator[tuple[str | None, ...]]:
    assert tenant_id == "tenant-a"
    yield tuple("unit-receipt" for _ in effects)


def _unit_state_effect(*, tenant_id: str, **kwargs: object) -> GolEffect:
    assert tenant_id == "tenant-a"
    return GolEffect.model_validate({**kwargs, "target": "test::CausalRun"})


@pytest.fixture(autouse=True)
def _admitted_sql_transport(request: pytest.FixtureRequest) -> Iterator[None]:
    """Keeps SQL-shape assertions separate from the real denial boundary tests."""
    if (
        request.node.name
        == "test_causal_cache_denial_performs_no_database_read_or_write"
    ):
        yield
        return
    with (
        patch(
            "galadril_vision.causal.runner.protected_gol_state_effect",
            new=_unit_scope,
        ),
        patch("galadril_vision.causal.runner.active_gol_lease"),
        patch(
            "galadril_vision.causal.runner.scoped_instance_id", side_effect=str
        ),
        patch(
            "galadril_vision.causal.runner.make_gol_state_effect",
            new=_unit_state_effect,
        ),
        patch(
            "galadril_vision.causal.runner.protected_gol_effects",
            new=_unit_effects,
        ),
    ):
        yield


@pytest.mark.anyio
async def test_unsigned_causal_cache_hit_is_never_returned() -> None:
    """A row indexed under the correct tenant and target still needs a receipt."""
    client = _Client()
    with patch.object(
        _Cursor, "fetchone", return_value=({"status": "success"},)
    ):
        with pytest.raises(GolDenied, match="Causal evidence unavailable"):
            await _cache_get(
                cast(PostgresClient, client), "tenant-a", "cache", "entity:one"
            )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
