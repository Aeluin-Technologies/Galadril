"""Actor-local real-time implementations for configured pipeline steps."""

from __future__ import annotations

import gc
from collections.abc import Sequence
from types import SimpleNamespace
from typing import cast

import orjson
import structlog
from galadril_inference.common.types import PredictionRequest
from galadril_ontology import (
    BlockOntologyContract,
    OntologyError,
    OntologyRuntimeManager,
    OntologySliceRequest,
    ResourceKind,
)
from galadril_pipeline.config import PipelineConfig, PipelineStep, StepType
from galadril_pipeline.events import PipelineCommand
from pydantic import JsonValue, TypeAdapter

from galadril_vision.actors.inference import get_inference_engine
from galadril_vision.causal.runner import (
    AmarthCausalRunner,
    build_slice_spec_from_step_params,
)
from galadril_vision.common.config import VisionConfig
from galadril_vision.compute.helpers import (
    _build_raw_data_record,
    _decode_raw_content,
    _extract_text_payload,
    _infer_modality,
    _storage_location,
)
from galadril_vision.compute.tasks import (
    PostgresRuntimeState,
    get_pg_stores,
    resolve_entities_batch,
    sink_to_db_batch,
)
from galadril_vision.connectors.s3.client import S3Client
from galadril_vision.identity.licorne import LicorneActorRuntime

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
logger = structlog.get_logger(__name__)


class CommandProcessingError(RuntimeError):
    """Raised for non-retryable command or pipeline configuration errors."""


class VisionCommandProcessor:
    """Reuses actor-local models, object storage, and database connection pools."""

    __slots__ = (
        "_configs",
        "_identity_runtime",
        "_ontology_runtime",
        "_pipelines",
        "_postgres_state",
        "_s3_client",
        "_steps",
    )

    def __init__(
        self,
        config: VisionConfig | Sequence[VisionConfig],
        *,
        ontology_runtime: OntologyRuntimeManager | None = None,
    ) -> None:
        """Compiles actor-local immutable configuration lookup tables."""
        configs = (
            (config,) if isinstance(config, VisionConfig) else tuple(config)
        )
        if not configs:
            raise ValueError("At least one pipeline configuration is required")
        self._configs = {item.name: item for item in configs}
        if len(self._configs) != len(configs):
            raise ValueError("Pipeline execution identities must be unique")
        self._pipelines: dict[str, PipelineConfig] = {
            item.name: item.to_pipeline_config() for item in configs
        }
        self._steps = {
            name: {step.step: step for step in pipeline.pipeline}
            for name, pipeline in self._pipelines.items()
        }
        self._postgres_state = PostgresRuntimeState()
        self._s3_client: S3Client | None = None
        self._identity_runtime: LicorneActorRuntime | None = None
        self._ontology_runtime = ontology_runtime

    async def process(self, command: PipelineCommand) -> dict[str, JsonValue]:
        """Runs one configured step and returns a Kafka-safe JSON object."""
        try:
            return await self._process_isolated(command)
        finally:
            self._sanitize(command)

    async def _process_isolated(
        self, command: PipelineCommand
    ) -> dict[str, JsonValue]:
        """Executes one command while the actor owns its request-local state."""
        config, step = self._step(command)
        if self._ontology_runtime is None:
            return await self._dispatch(command, step, config)
        request = OntologySliceRequest(
            tenant_id=command.tenant_id,
            pipeline_id=config.ontology_pipeline_id,
            block_id=command.step,
        )
        try:
            async with self._ontology_runtime.bind(
                request, _ontology_contract(step)
            ):
                return await self._dispatch(command, step, config)
        except OntologyError as error:
            raise CommandProcessingError(
                f"Block ontology unavailable or incompatible: {error}"
            ) from error

    @staticmethod
    def _sanitize(command: PipelineCommand) -> None:
        """Drops request-local Python and accelerator state after every command."""
        # Context variables and native resolver buffers can retain request data
        # after an exception. LI-ESKG scrubs its own leased workspace and keeps
        # only explicitly tenant-isolated durable runtimes between invocations.
        structlog.contextvars.clear_contextvars()
        collected = gc.collect()
        if command.resource_class.value == "gpu":
            try:
                import torch
            except ImportError:
                logger.warning("ray_actor_gpu_cache_cleanup_unavailable")
            else:
                torch.cuda.empty_cache()
        logger.debug(
            "ray_actor_command_sanitized",
            collected_objects=collected,
            resource_class=command.resource_class.value,
        )

    async def _dispatch(
        self,
        command: PipelineCommand,
        step: PipelineStep,
        config: VisionConfig,
    ) -> dict[str, JsonValue]:
        """Dispatches only after the tenant ontology context has been bound."""
        match step.type:
            case StepType.INFERENCE:
                return await self._process_inference(command, step, config)
            case StepType.RESOLVE:
                return await self._process_resolve(command, step, config)
            case StepType.SINK:
                return await self._process_sink(command, step, config)
            case StepType.CAUSAL:
                return await self._process_causal(command, step, config)
            case StepType.DBT:
                raise CommandProcessingError(
                    "dbt steps require a dedicated event-driven dbt adapter"
                )
        raise CommandProcessingError(f"Unsupported step type: {step.type}")

    def _step(
        self, command: PipelineCommand
    ) -> tuple[VisionConfig, PipelineStep]:
        """Prevents a command from selecting behavior outside its route contract."""
        config = self._configs.get(command.pipeline)
        pipeline = self._pipelines.get(command.pipeline)
        steps = self._steps.get(command.pipeline)
        if (
            config is None
            or pipeline is None
            or steps is None
            or not config.accepts_command(command.tenant_id, command.pipeline)
        ):
            raise CommandProcessingError(
                "Command does not match a loaded tenant or revision"
            )
        try:
            step = steps[command.step]
        except KeyError as error:
            raise CommandProcessingError(
                f"Unknown configured step: '{command.step}'"
            ) from error
        if command.step_type is not step.type:
            raise CommandProcessingError(
                f"Command step type '{command.step_type}' does not match '{step.type}'"
            )
        return config, step

    async def _process_inference(
        self,
        command: PipelineCommand,
        step: PipelineStep,
        config: VisionConfig,
    ) -> dict[str, JsonValue]:
        """Downloads one payload and executes GPU model inference in the actor."""
        if step.model is None:
            raise CommandProcessingError(
                f"Inference step '{step.step}' has no model"
            )
        record = _required_object(command.payload, "record")
        raw_data = await self._load_raw_data(record, config, command.tenant_id)
        params = step.params.model_extra or {}
        action = str(params.get("action") or "embed")
        engine = await get_inference_engine(
            model_name=step.model,
            models_bucket=config.models_store.bucket,
            models_prefix=config.models_store.prefix,
            endpoint_url=config.models_store.endpoint_url,
        )
        modality = str(raw_data.get("modality") or "data")
        data = raw_data.get("data")
        features = {
            "action": action,
            "data": data,
            "modality": modality,
            "mime_type": raw_data.get("mime_type"),
            "storage_path": raw_data.get("storage_path"),
            "metadata": raw_data.get("metadata") or {},
            "raw_payload": raw_data.get("raw_payload") or {},
        }
        if modality in {"image", "text", "audio", "video"}:
            features[modality] = data
        prediction = engine.predict(
            PredictionRequest(model_name=step.model, features=features)
        )
        output = {
            "record": record,
            "data": {
                "prediction": prediction.prediction,
                "confidence": prediction.confidence,
                "raw_modality": modality,
                "model_name": prediction.model_name,
                "model_version": prediction.model_version,
                "error": None,
            },
        }
        return _json_object(output)

    async def _load_raw_data(
        self,
        record: dict[str, JsonValue],
        config: VisionConfig,
        tenant_id: str,
    ) -> dict[str, object]:
        """Loads inline text or S3 bytes once inside the long-lived actor."""
        raw_payload = record.get("raw_payload")
        metadata = record.get("metadata")
        storage_path = record.get("storage_path")
        record_id = record.get("record_id")
        modality = _infer_modality(storage_path, raw_payload, metadata)
        inline_text = _extract_text_payload(raw_payload)
        mime_type = _mime_type(raw_payload, metadata)
        if inline_text is not None:
            return _build_raw_data_record(
                record_id=record_id,
                storage_path=storage_path,
                raw_payload=raw_payload,
                metadata=metadata,
                content=inline_text,
                modality="text" if modality == "data" else modality,
                mime_type=mime_type or "text/plain",
            )
        if not isinstance(storage_path, str) or not storage_path:
            raise CommandProcessingError(
                f"Record '{record_id}' has neither inline content nor storage_path"
            )
        if self._s3_client is None:
            store = config.raw_store
            self._s3_client = S3Client(
                bucket=store.bucket,
                endpoint_url=store.endpoint_url,
                aws_access_key=store.access_key,
                aws_secret_key=store.secret_key,
                aws_region=store.region_name,
            )
            await self._s3_client.connect()
        bucket, key = _storage_location(
            storage_path,
            config.raw_store.bucket,
            config.raw_store.prefix,
        )
        if bucket != config.raw_store.bucket:
            raise CommandProcessingError(
                "Raw object bucket is outside the configured tenant store"
            )
        _require_tenant_storage_key(key, tenant_id)
        (
            content,
            stored_mime_type,
        ) = await self._s3_client.get_object_with_metadata(
            key, target_bucket=bucket
        )
        effective_mime_type = stored_mime_type or mime_type
        decoded = _decode_raw_content(
            content, modality, effective_mime_type, record_id
        )
        return _build_raw_data_record(
            record_id=record_id,
            storage_path=storage_path,
            raw_payload=raw_payload,
            metadata=metadata,
            content=decoded,
            modality=modality,
            mime_type=effective_mime_type,
        )

    async def _process_resolve(
        self,
        command: PipelineCommand,
        step: PipelineStep,
        config: VisionConfig,
    ) -> dict[str, JsonValue]:
        """Resolves a single inference result using the actor-local vector pool."""
        record = _required_object(command.payload, "record")
        inference = _required_object(command.payload, "data")
        params = step.params.model_extra or {}
        identity_config = config.identity_resolution
        if identity_config.enabled and self._identity_runtime is None:
            self._identity_runtime = LicorneActorRuntime(identity_config)
        resolved = await resolve_entities_batch(
            state=self._postgres_state,
            postgres_config=config.postgres,
            inference_results=[cast(dict[str, object], inference)],
            tenant_ids=[command.tenant_id],
            modality=str(params.get("modality") or "data"),
            threshold=float(
                params.get("threshold")
                or identity_config.vector_similarity_midpoint
            ),
            resolver=self._identity_runtime,
            records=[cast(dict[str, object], record)],
            candidate_top_k=int(
                params.get("candidate_top_k") or identity_config.candidate_top_k
            ),
        )
        return _json_object({"record": record, "data": resolved[0]})

    async def _process_sink(
        self,
        command: PipelineCommand,
        step: PipelineStep,
        config: VisionConfig,
    ) -> dict[str, JsonValue]:
        """Persists one resolved record in a database transaction."""
        record = _required_object(command.payload, "record")
        resolved = command.payload.get("data")
        if not isinstance(resolved, list):
            raise CommandProcessingError(
                "Sink command data must be a JSON array"
            )
        params = step.params.model_extra or {}
        success = await sink_to_db_batch(
            state=self._postgres_state,
            postgres_config=config.postgres,
            resolved_items=[cast(list[dict[str, object]], resolved)],
            record_ids=[str(record.get("record_id") or command.event_id)],
            sources=[str(record.get("source") or "unknown")],
            tenant_ids=[command.tenant_id],
            event_types=[str(record.get("event_type") or "Observation")],
            raw_payloads=[
                cast(dict[str, object], record.get("raw_payload") or {})
            ],
            event_times=[cast(str | None, record.get("timestamp"))],
            spatials=[cast(dict[str, object] | None, record.get("spatial"))],
            entity_type=str(params.get("entity_type") or "ENTITY"),
            modality=str(params.get("modality") or "data"),
            edge_type=str(params.get("edge_type") or "APPEARS_IN"),
            state_type=str(params.get("state_type") or "observation"),
        )
        return _json_object(
            {"record": record, "data": {"persisted": success[0]}}
        )

    async def _process_causal(
        self,
        command: PipelineCommand,
        step: PipelineStep,
        config: VisionConfig,
    ) -> dict[str, JsonValue]:
        """Runs a scheduled causal slice using actor-local database resources."""
        client, _, graph = await get_pg_stores(
            config.postgres, self._postgres_state
        )
        params = dict(step.params.model_extra or {})
        target = command.payload.get("target")
        if isinstance(target, str):
            params["target"] = target
        namespace = SimpleNamespace(**params)
        spec = build_slice_spec_from_step_params(namespace)
        outcome = str(params.get("amarth_target_outcome") or "")
        if not outcome:
            raise CommandProcessingError(
                f"Causal step '{step.step}' requires amarth_target_outcome"
            )
        result = await AmarthCausalRunner(
            pg=client,
            graph=graph,
            tenant_id=command.tenant_id,
            spec=spec,
            target_outcome=outcome,
            window_size=str(params.get("amarth_window_size") or "14D"),
        ).run()
        return _json_object({"data": result})


def _required_object(
    payload: dict[str, JsonValue], key: str
) -> dict[str, JsonValue]:
    """Returns a required JSON object field with a domain-specific error."""
    value = payload.get(key)
    if not isinstance(value, dict):
        raise CommandProcessingError(
            f"Command payload '{key}' must be an object"
        )
    return value


def _require_tenant_storage_key(key: str, tenant_id: str) -> None:
    """Requires an exact tenant component below the optional raw prefix."""
    components = iter(part for part in key.split("/") if part)
    owner = next(components, None)
    if owner == "raw":
        owner = next(components, None)
    if owner != tenant_id:
        raise CommandProcessingError(
            "Raw object key is outside the command tenant partition"
        )


_DEFAULT_ONTOLOGY_KINDS: dict[StepType, tuple[ResourceKind, ...]] = {
    StepType.INFERENCE: tuple(ResourceKind),
    StepType.RESOLVE: (
        ResourceKind.OBJECT_TYPE,
        ResourceKind.PROPERTY,
        ResourceKind.LINK_TYPE,
    ),
    StepType.SINK: (
        ResourceKind.OBJECT_TYPE,
        ResourceKind.EVENT_TYPE,
        ResourceKind.PROPERTY,
        ResourceKind.LINK_TYPE,
    ),
    StepType.DBT: (
        ResourceKind.OBJECT_TYPE,
        ResourceKind.PROPERTY,
        ResourceKind.LINK_TYPE,
    ),
    StepType.CAUSAL: (
        ResourceKind.OBJECT_TYPE,
        ResourceKind.EVENT_TYPE,
        ResourceKind.PROPERTY,
        ResourceKind.LINK_TYPE,
    ),
}


def _ontology_contract(step: PipelineStep) -> BlockOntologyContract:
    """Compiles explicit block requirements with safe step-kind defaults."""
    parameters = step.params.model_extra or {}
    required_value = parameters.get("ontology_required_resources", ())
    allowed_value = parameters.get("ontology_allowed_kinds")
    if not isinstance(required_value, list | tuple) or not all(
        isinstance(item, str) for item in required_value
    ):
        raise CommandProcessingError(
            "ontology_required_resources must be an array of resource identifiers"
        )
    if allowed_value is None:
        allowed_kinds = _DEFAULT_ONTOLOGY_KINDS[step.type]
    elif isinstance(allowed_value, list | tuple) and all(
        isinstance(item, str) for item in allowed_value
    ):
        try:
            allowed_kinds = tuple(ResourceKind(item) for item in allowed_value)
        except ValueError as error:
            raise CommandProcessingError(
                "ontology_allowed_kinds contains an unsupported resource kind"
            ) from error
    else:
        raise CommandProcessingError(
            "ontology_allowed_kinds must be an array of resource kinds"
        )
    return BlockOntologyContract(
        required_resource_ids=tuple(
            cast(list[str] | tuple[str, ...], required_value)
        ),
        allowed_kinds=allowed_kinds,
    )


def _mime_type(*values: JsonValue | None) -> str | None:
    """Extracts the first supported MIME field from JSON objects."""
    for value in values:
        if not isinstance(value, dict):
            continue
        candidate = value.get("mime_type") or value.get("content_type")
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _json_object(value: object) -> dict[str, JsonValue]:
    """Converts NumPy/model objects once at the Kafka serialization boundary."""
    encoded = orjson.dumps(value, option=orjson.OPT_SERIALIZE_NUMPY)
    return _JSON_OBJECT.validate_python(orjson.loads(encoded))
