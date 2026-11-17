"""Unit tests for the actor-local LI-ESKG integration boundary."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import patch

import orjson
import pytest
from galadril_ontology import GolDenied, GolEffect
from galadril_vision.common.config import IdentityResolutionConfig
from galadril_vision.identity.contracts import (
    CandidateEvidence,
    ResolutionRequest,
    SpatialEvidence,
)
from galadril_vision.identity.licorne import (
    LicorneActorRuntime,
    _bounded_probability,
    _CalibratedPostgresProvider,
    _load_licorne,
    _stable_u32,
    _stable_u64,
)


def test_native_binding_is_packaged_with_identity_runtime() -> None:
    """Ensures Ray actors can import the native resolver from image runfiles."""
    module = _load_licorne()
    assert hasattr(module, "AsyncResolver")


class _Candidate:
    """Minimal native candidate shape used by the provider protocol."""

    def __init__(
        self,
        *,
        identity_id: int | None = None,
        backend: int | None = None,
        key: str | None = None,
    ) -> None:
        self.identity_id = identity_id
        self.backend = backend
        self.key = key

    @staticmethod
    def latent(identity_id: int) -> _Candidate:
        return _Candidate(identity_id=identity_id)

    @staticmethod
    def known(backend: int, key: str, snapshot: int) -> _Candidate:
        del snapshot
        return _Candidate(backend=backend, key=key)


class _H3Index:
    """Deterministic in-memory substitute for the native H3 index."""

    def __init__(self, resolution: int, ring_size: int) -> None:
        self.resolution = resolution
        self.ring_size = ring_size
        self.identities: set[int] = set()

    def register(
        self, identity_id: int, point: tuple[float, float], accuracy: float
    ) -> int:
        del point, accuracy
        self.identities.add(identity_id)
        return identity_id

    def query(self, point: tuple[float, float], accuracy: float) -> list[int]:
        del point, accuracy
        return list(self.identities)

    def remove(self, identity_id: int) -> bool:
        if identity_id not in self.identities:
            return False
        self.identities.remove(identity_id)
        return True

    def cell(self, point: tuple[float, float]) -> int:
        return int((point[0] + 90.0) * 1_000_000 + point[1] + 180.0)


@dataclass(slots=True)
class _FactorTable:
    variables: list[int]
    cardinalities: list[int]
    log_potentials: list[float]
    contributions: list[object] | None = None


@dataclass(slots=True)
class _ScoreContribution:
    value: float
    semantics: str
    provider_id: int
    model_version: int
    calibration_id: int
    validity_domain: str


class _Observation:
    def __init__(
        self,
        identifier: int,
        source: int,
        modality: int,
        event_time: int,
        payload: bytes,
    ) -> None:
        self.id = identifier
        self.source = source
        self.modality = modality
        self.event_time = event_time
        self.payload = payload


class _Resolution:
    action = "create"
    identity_id = 7
    host_reference = None
    created_identity = True
    probabilities = [("new", 0.91), ("noise", 0.09)]
    decision_id = 11
    inference_id = 12
    iterations = 1
    residual = 0.0
    exact = True


@dataclass(slots=True)
class _Batch:
    tickets: list[int]
    resolutions: list[_Resolution]
    final_version: int | None = 13
    error: str | None = None


class _AsyncResolver:
    """Queue-backed stand-in exercising runtime ticket correlation."""

    def __init__(self, **kwargs: object) -> None:
        self.providers = kwargs["providers"]
        self.closed = False
        self._queue: asyncio.Queue[_Batch] = asyncio.Queue()
        self._ticket = 0

    def register_tenant(self, tenant_id: str, **kwargs: object) -> None:
        del tenant_id, kwargs

    def submit(self, observation: _Observation, *, tenant_id: str) -> int:
        del observation, tenant_id
        self._ticket += 1
        return self._ticket

    def flush(self, *, tenant_id: str) -> None:
        del tenant_id
        self._queue.put_nowait(_Batch([self._ticket], [_Resolution()]))

    async def next_result(self) -> _Batch:
        return await self._queue.get()

    def close(self) -> None:
        self.closed = True
        self._queue.put_nowait(_Batch([], []))

    async def wait_closed(self) -> None:
        return None


def _licorne() -> SimpleNamespace:
    return SimpleNamespace(
        AsyncResolver=_AsyncResolver,
        Candidate=_Candidate,
        FactorTable=_FactorTable,
        GeoPoint=lambda latitude, longitude: (latitude, longitude),
        H3CandidateIndex=_H3Index,
        Observation=_Observation,
        ScoreContribution=_ScoreContribution,
    )


def test_provider_preserves_identity_kind_and_emits_finite_evidence() -> None:
    """Maps linked candidates to latent IDs and cold PostgreSQL rows to hosts."""
    provider = _CalibratedPostgresProvider(
        _licorne(), IdentityResolutionConfig()
    )
    observation = _Observation(
        1,
        2,
        3,
        4,
        orjson.dumps(
            {
                "pipeline_probability": 0.9,
                "vector_similarity_midpoint": 0.85,
                "candidates": [
                    {
                        "entity_id": "person-linked",
                        "similarity": 0.95,
                        "licorne_identity_id": 42,
                    },
                    {
                        "entity_id": "person-host",
                        "similarity": 0.9,
                        "licorne_identity_id": None,
                    },
                ],
                "spatial": None,
            }
        ),
    )

    candidates = provider.generate_candidates([observation], object())
    factors = provider.emit_factors([observation], candidates, object())

    assert candidates[0][0].identity_id == 42
    assert candidates[0][1].key == "person-host"
    assert factors[0].cardinalities == [4]
    assert len(factors[0].contributions or []) == 2
    assert factors[0].log_potentials[0] > factors[0].log_potentials[1]


@pytest.mark.anyio
async def test_actor_runtime_correlates_native_decision_and_h3_cell() -> None:
    """Returns stable IDs and normalized durable decision metadata."""
    runtime = LicorneActorRuntime(
        IdentityResolutionConfig(result_timeout_seconds=1.0),
        licorne_module=_licorne(),
    )
    request = ResolutionRequest(
        tenant_id="tenant-1",
        observation_key="record-1:face:0",
        source="camera-1",
        modality="face",
        event_time_micros=1_000_000,
        pipeline_probability=0.9,
        candidates=(CandidateEvidence("candidate", 0.95),),
        spatial=SpatialEvidence(51.5074, -0.1278, 10.0),
    )

    decision = await runtime.resolve(request)
    await runtime.close()

    assert decision.entity_id == "licorne_0000000000000007"
    assert decision.licorne_identity_id == 7
    assert decision.selected_probability == 0.91
    assert decision.h3_cell is not None
    assert decision.final_version == 13


@pytest.mark.anyio
async def test_native_runtime_creates_when_h3_excludes_far_candidate() -> None:
    """Exercises the compiled LI-ESKG scheduler and H3 candidate gate."""
    native = pytest.importorskip("licorne")
    runtime = LicorneActorRuntime(
        IdentityResolutionConfig(
            max_batch_latency_ms=1,
            result_timeout_seconds=2.0,
        ),
        licorne_module=native,
    )
    request = ResolutionRequest(
        tenant_id="native-h3-test",
        observation_key="record-1:face:0",
        source="camera-1",
        modality="face",
        event_time_micros=1_000_000,
        pipeline_probability=0.99,
        candidates=(
            CandidateEvidence(
                "new-york-person",
                0.99,
                spatial=SpatialEvidence(40.7128, -74.0060, 10.0),
            ),
        ),
        spatial=SpatialEvidence(51.5074, -0.1278, 10.0),
    )

    try:
        decision = await runtime.resolve(request)
    finally:
        await runtime.close()

    assert decision.action == "create"
    assert decision.created_identity is True
    assert decision.h3_cell is not None


def test_probability_and_identifier_boundaries_are_strict() -> None:
    """Rejects invalid probabilities and keeps canonical hashes replay-stable."""
    assert _stable_u64("tenant", "record") == _stable_u64("tenant", "record")
    assert _stable_u64("tenant", "record") != _stable_u64("tenant", "other")
    assert 0 < _stable_u32("modality", "face") <= 2**32 - 1
    with pytest.raises(ValueError, match="probability"):
        _bounded_probability(1.1, 1.0e-6)


@pytest.fixture
def anyio_backend() -> str:
    """Runs async contracts on the production asyncio backend."""
    return "asyncio"


@pytest.mark.anyio
async def test_missing_state_admission_does_not_initialize_native_ledger() -> (
    None
):
    runtime = LicorneActorRuntime(
        IdentityResolutionConfig(result_timeout_seconds=1.0),
        licorne_module=_licorne(),
    )
    request = ResolutionRequest(
        tenant_id="tenant-1",
        observation_key="observation",
        source="sensor",
        modality="text",
        event_time_micros=1,
        pipeline_probability=0.8,
        candidates=(),
    )
    try:
        with pytest.raises(GolDenied):
            await runtime.resolve(request)
        assert runtime._resolver is None
        assert not runtime._providers
    finally:
        await runtime.close()


@asynccontextmanager
async def _unit_batch(
    effects: tuple[GolEffect, ...], **_kwargs: object
) -> AsyncIterator[tuple[str, ...]]:
    """The native unit adapter exercises I/O ordering independently of policy engines."""
    yield tuple("unit-receipt" for _ in effects)


def _unit_state_effect(**kwargs: object) -> GolEffect:
    return GolEffect.model_validate(
        {
            key: value
            for key, value in kwargs.items()
            if key not in {"tenant_id", "derived"}
        }
        | {"target": "tests.scope::Value"}
    )


@pytest.fixture(autouse=True)
def _admitted_sql_transport(request: pytest.FixtureRequest) -> Iterator[None]:
    """Keeps SQL-shape assertions separate from the real denial boundary tests."""
    if (
        request.node.name
        == "test_missing_state_admission_does_not_initialize_native_ledger"
    ):
        yield
        return
    with (
        patch(
            "galadril_vision.identity.licorne.protected_gol_effects",
            new=_unit_batch,
        ),
        patch(
            "galadril_vision.identity.licorne.make_gol_state_effect",
            new=_unit_state_effect,
        ),
    ):
        yield


@pytest.mark.anyio
async def test_native_ledger_io_and_results_remain_inside_the_live_effect_scope() -> (
    None
):
    """Native background polling cannot outlive the admitted calculation."""
    held = False

    @asynccontextmanager
    async def scope(
        *_args: object, **_kwargs: object
    ) -> AsyncIterator[tuple[str, ...]]:
        nonlocal held
        held = True
        try:
            yield ("read-receipt", "write-receipt")
        finally:
            held = False

    class Resolver(_AsyncResolver):
        def register_tenant(self, tenant_id: str, **kwargs: object) -> None:
            assert held, "Native ledger escaped its live permit"
            super().register_tenant(tenant_id, **kwargs)

        async def next_result(self) -> _Batch:
            assert held, "Native result escaped its live permit"
            return await super().next_result()

    module = _licorne()
    module.AsyncResolver = Resolver
    runtime = LicorneActorRuntime(
        IdentityResolutionConfig(result_timeout_seconds=1.0),
        licorne_module=module,
    )
    request = ResolutionRequest(
        tenant_id="tenant-a",
        observation_key="record",
        source="source",
        modality="text",
        event_time_micros=1,
        pipeline_probability=0.9,
        candidates=(),
    )
    with (
        patch(
            "galadril_vision.identity.licorne.protected_gol_effects",
            new=scope,
            create=True,
        ),
        patch(
            "galadril_vision.identity.licorne.make_gol_state_effect",
            return_value=object(),
            create=True,
        ),
    ):
        try:
            await runtime.resolve(request)
            assert runtime._pump is None or runtime._pump.done()
        finally:
            await runtime.close()
    assert not held


if __name__ == "__main__":
    raise SystemExit(
        pytest.main([__file__, "--import-mode=importlib", *sys.argv[1:]])
    )
