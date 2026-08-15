"""Actor-local LI-ESKG adapter."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import math
from dataclasses import dataclass
from typing import Any, Protocol, cast

import orjson
import structlog

from galadril_vision.common.config import IdentityResolutionConfig

logger = structlog.get_logger(__name__)


class IdentityResolutionError(RuntimeError):
    """Raised when the native resolver cannot produce a durable decision."""


@dataclass(frozen=True, slots=True)
class SpatialEvidence:
    """WGS84 point and one-sigma location uncertainty."""

    latitude: float
    longitude: float
    accuracy_meters: float = 0.0


@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    """One PostgreSQL candidate and its optional LI-ESKG identity mapping."""

    entity_id: str
    similarity: float
    licorne_identity_id: int | None = None
    spatial: SpatialEvidence | None = None


@dataclass(frozen=True, slots=True)
class ResolutionRequest:
    """Immutable evidence submitted by a Vision actor for one extracted entity."""

    tenant_id: str
    observation_key: str
    source: str
    modality: str
    event_time_micros: int
    pipeline_probability: float
    candidates: tuple[CandidateEvidence, ...]
    spatial: SpatialEvidence | None = None
    vector_similarity_midpoint: float | None = None


@dataclass(frozen=True, slots=True)
class ResolutionDecision:
    """Normalized LI-ESKG result with the canonical PostgreSQL entity ID."""

    entity_id: str | None
    action: str
    licorne_identity_id: int | None
    observation_id: int
    decision_id: int
    inference_id: int
    probabilities: tuple[tuple[str, float], ...]
    selected_probability: float | None
    created_identity: bool
    final_version: int | None
    h3_cell: int | None
    iterations: int
    residual: float
    exact: bool


class IdentityResolver(Protocol):
    """Async resolution contract consumed by the compute pipeline."""

    async def resolve(self, request: ResolutionRequest) -> ResolutionDecision:
        """Resolves one observation against a bounded candidate domain."""
        ...


class _CalibratedPostgresProvider:
    """LI-ESKG provider over pre-fetched PostgreSQL candidate evidence."""

    __slots__ = ("_config", "_h3", "_licorne")

    def __init__(self, licorne: Any, config: IdentityResolutionConfig) -> None:
        self._licorne = licorne
        self._config = config
        self._h3 = licorne.H3CandidateIndex(
            config.h3_resolution, config.h3_ring_size
        )

    @property
    def provider_id(self) -> int:
        return self._config.provider_id

    @property
    def schema_id(self) -> int:
        return self._config.schema_id

    @property
    def model_version(self) -> int:
        return self._config.model_version

    @property
    def calibration_id(self) -> int:
        return self._config.calibration_id

    def generate_candidates(
        self, observations: list[Any], context: Any
    ) -> list[list[Any]]:
        """Builds stable latent or authoritative-host candidate references."""
        del context
        groups: list[list[Any]] = []
        for observation in observations:
            payload = self._payload(observation)
            group: list[Any] = []
            for candidate in self._spatially_gated(payload):
                licorne_id = candidate.get("licorne_identity_id")
                if isinstance(licorne_id, int):
                    group.append(self._licorne.Candidate.latent(licorne_id))
                    continue
                entity_id = candidate.get("entity_id")
                if not isinstance(entity_id, str) or not entity_id:
                    continue
                group.append(
                    self._licorne.Candidate.known(
                        self._config.postgres_backend_id,
                        entity_id,
                        self._config.host_snapshot,
                    )
                )
            groups.append(group)
        return groups

    def emit_factors(
        self,
        observations: list[Any],
        candidates: list[list[Any]],
        context: Any,
    ) -> list[Any]:
        """Emits finite log-domain evidence without treating H3 as a model."""
        del context
        factors: list[Any] = []
        for variable, (observation, domain) in enumerate(
            zip(observations, candidates, strict=True)
        ):
            if not domain:
                continue
            payload = self._payload(observation)
            evidence = {
                self._candidate_key(item): item
                for item in self._spatially_gated(payload)
            }
            pipeline_probability = _bounded_probability(
                payload.get("pipeline_probability"),
                self._config.probability_epsilon,
            )
            midpoint = _finite_float(
                payload.get("vector_similarity_midpoint"),
                self._config.vector_similarity_midpoint,
            )
            pipeline_llr = _log_odds(
                pipeline_probability, self._config.probability_epsilon
            )
            potentials: list[float] = []
            similarity_llrs: list[float] = []
            for candidate in domain:
                item = evidence.get(self._domain_key(candidate))
                similarity = _finite_float(
                    None if item is None else item.get("similarity"), -1.0
                )
                similarity_probability = _sigmoid(
                    self._config.vector_similarity_scale
                    * (similarity - midpoint)
                )
                similarity_llr = _log_odds(
                    similarity_probability, self._config.probability_epsilon
                )
                combined = (
                    self._config.vector_weight * similarity_llr
                    + self._config.pipeline_probability_weight * pipeline_llr
                )
                bounded = max(
                    -self._config.max_abs_log_likelihood_ratio,
                    min(self._config.max_abs_log_likelihood_ratio, combined),
                )
                potentials.append(bounded)
                similarity_llrs.append(similarity_llr)
            potentials.extend(
                (
                    self._config.new_evidence_log_potential,
                    self._config.noise_evidence_log_potential,
                )
            )
            contributions = [
                self._licorne.ScoreContribution(
                    pipeline_llr,
                    "log_likelihood_ratio",
                    self.provider_id,
                    self.model_version,
                    self.calibration_id,
                    "galadril/pipeline-probability",
                ),
                self._licorne.ScoreContribution(
                    max(similarity_llrs),
                    "log_likelihood_ratio",
                    self.provider_id,
                    self.model_version,
                    self.calibration_id,
                    "galadril/pgvector-cosine-calibration",
                ),
            ]
            factors.append(
                self._licorne.FactorTable(
                    [variable],
                    [len(potentials)],
                    potentials,
                    contributions=contributions,
                )
            )
        return factors

    def h3_cell(self, spatial: SpatialEvidence | None) -> int | None:
        """Returns the native H3 cell for an observation point when present."""
        if spatial is None:
            return None
        point = self._licorne.GeoPoint(spatial.latitude, spatial.longitude)
        return int(self._h3.cell(point))

    def _spatially_gated(
        self, payload: dict[str, object]
    ) -> list[dict[str, object]]:
        """Uses H3 as a conservative gate only where both points are known."""
        raw_candidates = payload.get("candidates")
        if not isinstance(raw_candidates, list):
            return []
        candidates = [
            cast(dict[str, object], item)
            for item in raw_candidates
            if isinstance(item, dict)
        ]
        raw_spatial = payload.get("spatial")
        if not isinstance(raw_spatial, dict):
            return candidates
        point = self._point(raw_spatial)
        if point is None:
            return candidates

        keyed: dict[int, dict[str, object]] = {}
        registered_keys: list[int] = []
        candidates_without_location: list[dict[str, object]] = []
        for candidate in candidates:
            candidate_spatial = candidate.get("spatial")
            if not isinstance(candidate_spatial, dict):
                candidates_without_location.append(candidate)
                continue
            candidate_point = self._point(candidate_spatial)
            if candidate_point is None:
                candidates_without_location.append(candidate)
                continue
            entity_id = candidate.get("entity_id")
            if not isinstance(entity_id, str) or not entity_id:
                continue
            spatial_key = _stable_u64("h3", entity_id)
            accuracy = max(
                0.0,
                _finite_float(candidate_spatial.get("accuracy_meters"), 0.0),
            )
            self._h3.register(spatial_key, candidate_point, accuracy)
            keyed[spatial_key] = candidate
            registered_keys.append(spatial_key)

        observation_accuracy = max(
            0.0, _finite_float(raw_spatial.get("accuracy_meters"), 0.0)
        )
        try:
            allowed = set(self._h3.query(point, observation_accuracy))
        finally:
            for spatial_key in registered_keys:
                self._h3.remove(spatial_key)
        return candidates_without_location + [
            candidate for key, candidate in keyed.items() if key in allowed
        ]

    def _point(self, value: dict[str, object]) -> Any | None:
        latitude = _finite_float(value.get("latitude"), math.nan)
        longitude = _finite_float(value.get("longitude"), math.nan)
        if not math.isfinite(latitude) or not math.isfinite(longitude):
            return None
        try:
            return self._licorne.GeoPoint(latitude, longitude)
        except ValueError:
            return None

    def _candidate_key(self, candidate: dict[str, object]) -> str:
        licorne_id = candidate.get("licorne_identity_id")
        if isinstance(licorne_id, int):
            return f"identity:{licorne_id}"
        entity_id = candidate.get("entity_id")
        return f"known:{self._config.postgres_backend_id}:{entity_id}"

    @staticmethod
    def _domain_key(candidate: Any) -> str:
        identity_id = candidate.identity_id
        if identity_id is not None:
            return f"identity:{identity_id}"
        return f"known:{candidate.backend}:{candidate.key}"

    @staticmethod
    def _payload(observation: Any) -> dict[str, object]:
        payload = observation.payload
        if payload is None:
            raise ValueError("LI-ESKG observations require inline evidence")
        decoded = orjson.loads(payload)
        if not isinstance(decoded, dict):
            raise ValueError("LI-ESKG observation evidence must be an object")
        return cast(dict[str, object], decoded)


class LicorneActorRuntime:
    """Long-lived per-Ray-process scheduler for isolated tenant resolvers."""

    __slots__ = (
        "_config",
        "_licorne",
        "_lock",
        "_pending",
        "_providers",
        "_pump",
        "_resolver",
    )

    def __init__(
        self,
        config: IdentityResolutionConfig,
        *,
        licorne_module: Any | None = None,
    ) -> None:
        self._config = config
        self._licorne = licorne_module or _load_licorne()
        self._lock = asyncio.Lock()
        self._resolver: Any | None = None
        self._providers: dict[str, _CalibratedPostgresProvider] = {}
        self._pending: dict[int, asyncio.Future[tuple[Any, int | None]]] = {}
        self._pump: asyncio.Task[None] | None = None

    async def resolve(self, request: ResolutionRequest) -> ResolutionDecision:
        """Submits evidence and correlates the native batch result by ticket."""
        async with self._lock:
            provider = self._ensure_tenant(request.tenant_id)
            resolver = self._resolver
            if resolver is None:
                raise IdentityResolutionError(
                    "LI-ESKG resolver was not initialized"
                )
            observation_id = _stable_u64(
                request.tenant_id, request.observation_key
            )
            observation = self._licorne.Observation(
                observation_id,
                _stable_u64("source", request.source),
                _stable_u32("modality", request.modality),
                request.event_time_micros,
                self._payload(request),
            )
            loop = asyncio.get_running_loop()
            future: asyncio.Future[tuple[Any, int | None]] = (
                loop.create_future()
            )
            ticket = int(
                resolver.submit(observation, tenant_id=request.tenant_id)
            )
            self._pending[ticket] = future
            resolver.flush(tenant_id=request.tenant_id)
            if self._pump is None or self._pump.done():
                self._pump = asyncio.create_task(
                    self._pump_results(), name="licorne-result-pump"
                )

        try:
            resolution, final_version = await asyncio.wait_for(
                future, timeout=self._config.result_timeout_seconds
            )
        except TimeoutError as error:
            async with self._lock:
                self._pending.pop(ticket, None)
            raise IdentityResolutionError(
                f"LI-ESKG resolution timed out for ticket {ticket}"
            ) from error
        return self._decision(
            request, provider, resolution, final_version, observation_id
        )

    async def close(self) -> None:
        """Stops native workers and fails unresolved actor-local requests."""
        async with self._lock:
            resolver = self._resolver
            self._resolver = None
            if resolver is not None:
                resolver.close()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(
                        IdentityResolutionError("LI-ESKG resolver closed")
                    )
            self._pending.clear()
        if resolver is not None:
            await resolver.wait_closed()
        if self._pump is not None:
            await asyncio.gather(self._pump, return_exceptions=True)

    def _ensure_tenant(self, tenant_id: str) -> _CalibratedPostgresProvider:
        provider = self._providers.get(tenant_id)
        if provider is not None:
            return provider
        provider = _CalibratedPostgresProvider(self._licorne, self._config)
        if self._resolver is None:
            kwargs: dict[str, object] = {
                "providers": [provider],
                "tenant_id": tenant_id,
                "queue_capacity": self._config.queue_capacity,
                "result_capacity": self._config.result_capacity,
                "max_batch_size": self._config.max_batch_size,
                "max_batch_latency_ms": self._config.max_batch_latency_ms,
                "worker_threads": self._config.worker_threads,
                "pool_max_idle": self._config.pool_max_idle,
                "host_snapshot": self._config.host_snapshot,
                "candidate_snapshot": self._config.candidate_snapshot,
                "candidate_log_prior": self._config.candidate_log_prior,
                "new_log_prior": self._config.new_log_prior,
                "noise_log_prior": self._config.noise_log_prior,
            }
            if self._config.ledger_root is not None:
                kwargs["ledger_root"] = self._config.ledger_root
            self._resolver = self._licorne.AsyncResolver(**kwargs)
        else:
            self._resolver.register_tenant(
                tenant_id,
                providers=[provider],
                host_snapshot=self._config.host_snapshot,
                candidate_snapshot=self._config.candidate_snapshot,
            )
        self._providers[tenant_id] = provider
        return provider

    async def _pump_results(self) -> None:
        resolver = self._resolver
        if resolver is None:
            return
        active_resolver = cast(Any, resolver)
        try:
            while self._resolver is resolver and not active_resolver.closed:
                batch = await active_resolver.next_result()
                error = batch.error
                if error is not None:
                    failure = IdentityResolutionError(str(error))
                    for ticket in batch.tickets:
                        future = self._pending.pop(int(ticket), None)
                        if future is not None and not future.done():
                            future.set_exception(failure)
                    continue
                resolutions = batch.resolutions
                for ticket, resolution in zip(
                    batch.tickets, resolutions, strict=True
                ):
                    future = self._pending.pop(int(ticket), None)
                    if future is not None and not future.done():
                        future.set_result((resolution, batch.final_version))
        except Exception as error:
            if active_resolver.closed or self._resolver is not resolver:
                return
            failure = IdentityResolutionError(
                f"LI-ESKG result pump failed: {error}"
            )
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(failure)
            self._pending.clear()
            logger.exception(
                "licorne_result_pump_failed", error_type=type(error).__name__
            )

    def _payload(self, request: ResolutionRequest) -> bytes:
        candidates = []
        for candidate in request.candidates:
            encoded: dict[str, object] = {
                "entity_id": candidate.entity_id,
                "similarity": candidate.similarity,
                "licorne_identity_id": candidate.licorne_identity_id,
            }
            if candidate.spatial is not None:
                encoded["spatial"] = _spatial_dict(candidate.spatial)
            candidates.append(encoded)
        return orjson.dumps(
            {
                "pipeline_probability": request.pipeline_probability,
                "vector_similarity_midpoint": (
                    request.vector_similarity_midpoint
                    if request.vector_similarity_midpoint is not None
                    else self._config.vector_similarity_midpoint
                ),
                "candidates": candidates,
                "spatial": (
                    _spatial_dict(request.spatial)
                    if request.spatial is not None
                    else None
                ),
            }
        )

    def _decision(
        self,
        request: ResolutionRequest,
        provider: _CalibratedPostgresProvider,
        resolution: Any,
        final_version: int | None,
        observation_id: int,
    ) -> ResolutionDecision:
        probabilities = tuple(
            (str(label), float(probability))
            for label, probability in resolution.probabilities
        )
        entity_by_licorne = {
            item.licorne_identity_id: item.entity_id
            for item in request.candidates
            if item.licorne_identity_id is not None
        }
        licorne_identity_id = (
            int(resolution.identity_id)
            if resolution.identity_id is not None
            else None
        )
        entity_id: str | None = None
        selected_label: str | None = None
        if resolution.host_reference is not None:
            _, entity_id, _ = resolution.host_reference
            selected_label = (
                f"known:{self._config.postgres_backend_id}:{entity_id}"
            )
        elif licorne_identity_id is not None:
            entity_id = entity_by_licorne.get(
                licorne_identity_id,
                f"licorne_{licorne_identity_id:016x}",
            )
            selected_label = (
                "new"
                if bool(resolution.created_identity)
                else f"identity:{licorne_identity_id}"
            )
        elif str(resolution.action) == "reject_noise":
            selected_label = "noise"
        selected_probability = next(
            (
                probability
                for label, probability in probabilities
                if label == selected_label
            ),
            None,
        )
        return ResolutionDecision(
            entity_id=entity_id,
            action=str(resolution.action),
            licorne_identity_id=licorne_identity_id,
            observation_id=observation_id,
            decision_id=int(resolution.decision_id),
            inference_id=int(resolution.inference_id),
            probabilities=probabilities,
            selected_probability=selected_probability,
            created_identity=bool(resolution.created_identity),
            final_version=(
                int(final_version) if final_version is not None else None
            ),
            h3_cell=provider.h3_cell(request.spatial),
            iterations=int(resolution.iterations),
            residual=float(resolution.residual),
            exact=bool(resolution.exact),
        )


def _load_licorne() -> Any:
    """Loads the optional native dependency only inside a resolving actor."""
    try:
        return importlib.import_module("licorne")
    except ImportError as error:
        raise IdentityResolutionError(
            "LI-ESKG Python binding 'licorne' is required for resolve steps"
        ) from error


def _stable_u64(*parts: str) -> int:
    """Builds a deterministic non-zero unsigned identifier from canonical text."""
    digest = hashlib.blake2b(digest_size=8, person=b"galadril-li")
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return int.from_bytes(digest.digest(), "big") or 1


def _stable_u32(*parts: str) -> int:
    """Builds a deterministic non-zero modality identifier within Rust u32."""
    digest = hashlib.blake2s(digest_size=4, person=b"gala-li")
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return int.from_bytes(digest.digest(), "big") or 1


def _finite_float(value: object, default: float) -> float:
    """Returns a finite scalar or a caller-provided neutral fallback."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    converted = float(value)
    return converted if math.isfinite(converted) else default


def _bounded_probability(value: object, epsilon: float) -> float:
    """Validates probability semantics and bounds log-odds singularities."""
    probability = _finite_float(value, 0.5)
    if not 0.0 <= probability <= 1.0:
        raise ValueError("pipeline probability must be within [0.0, 1.0]")
    return max(epsilon, min(1.0 - epsilon, probability))


def _log_odds(probability: float, epsilon: float) -> float:
    """Converts a calibrated probability to finite log likelihood odds."""
    bounded = max(epsilon, min(1.0 - epsilon, probability))
    return math.log(bounded / (1.0 - bounded))


def _sigmoid(value: float) -> float:
    """Evaluates a numerically stable logistic calibration function."""
    if value >= 0.0:
        exponential = math.exp(-value)
        return 1.0 / (1.0 + exponential)
    exponential = math.exp(value)
    return exponential / (1.0 + exponential)


def _spatial_dict(spatial: SpatialEvidence) -> dict[str, float]:
    """Serializes fixed-size spatial evidence for the native provider boundary."""
    return {
        "latitude": spatial.latitude,
        "longitude": spatial.longitude,
        "accuracy_meters": spatial.accuracy_meters,
    }
