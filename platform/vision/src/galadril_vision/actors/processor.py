"""Actor-local real-time implementations for configured pipeline steps."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import orjson
from galadril_inference.common.types import PredictionRequest
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


class CommandProcessingError(RuntimeError):
    """Raised for non-retryable command or pipeline configuration errors."""


class VisionCommandProcessor:
    """Reuses actor-local models, object storage, and database connection pools."""

    __slots__ = (
        "_config",
        "_identity_runtime",
        "_pipeline",
        "_postgres_state",
        "_s3_client",
        "_steps",
    )

    def __init__(self, config: VisionConfig) -> None:
        """Compiles actor-local immutable configuration lookup tables."""
        self._config = config
        self._pipeline: PipelineConfig = config.to_pipeline_config()
        self._steps = {step.step: step for step in self._pipeline.pipeline}
        self._postgres_state = PostgresRuntimeState()
        self._s3_client: S3Client | None = None
        self._identity_runtime: LicorneActorRuntime | None = None

    async def process(self, command: PipelineCommand) -> dict[str, JsonValue]:
        """Runs one configured step and returns a Kafka-safe JSON object."""
        step = self._step(command)
        match step.type:
            case StepType.INFERENCE:
                return await self._process_inference(command, step)
            case StepType.RESOLVE:
                return await self._process_resolve(command, step)
            case StepType.SINK:
                return await self._process_sink(command, step)
            case StepType.CAUSAL:
                return await self._process_causal(command, step)
            case StepType.DBT:
                raise CommandProcessingError(
                    "dbt steps require a dedicated event-driven dbt adapter"
                )
        raise CommandProcessingError(f"Unsupported step type: {step.type}")

    def _step(self, command: PipelineCommand) -> PipelineStep:
        """Prevents a command from selecting behavior outside its route contract."""
        try:
            step = self._steps[command.step]
        except KeyError as error:
            raise CommandProcessingError(
                f"Unknown configured step: '{command.step}'"
            ) from error
        if command.pipeline != self._pipeline.name:
            raise CommandProcessingError(
                f"Command pipeline '{command.pipeline}' does not match "
                f"'{self._pipeline.name}'"
            )
        if command.step_type is not step.type:
            raise CommandProcessingError(
                f"Command step type '{command.step_type}' does not match '{step.type}'"
            )
        return step

    async def _process_inference(
        self, command: PipelineCommand, step: PipelineStep
    ) -> dict[str, JsonValue]:
        """Downloads one payload and executes GPU model inference in the actor."""
        if step.model is None:
            raise CommandProcessingError(
                f"Inference step '{step.step}' has no model"
            )
        record = _required_object(command.payload, "record")
        raw_data = await self._load_raw_data(record)
        params = step.params.model_extra or {}
        action = str(params.get("action") or "embed")
        engine = await get_inference_engine(
            model_name=step.model,
            models_bucket=self._config.models_store.bucket,
            models_prefix=self._config.models_store.prefix,
            endpoint_url=self._config.models_store.endpoint_url,
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
        self, record: dict[str, JsonValue]
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
            store = self._config.raw_store
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
            self._config.raw_store.bucket,
            self._config.raw_store.prefix,
        )
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
        self, command: PipelineCommand, step: PipelineStep
    ) -> dict[str, JsonValue]:
        """Resolves a single inference result using the actor-local vector pool."""
        record = _required_object(command.payload, "record")
        inference = _required_object(command.payload, "data")
        params = step.params.model_extra or {}
        identity_config = self._config.identity_resolution
        if identity_config.enabled and self._identity_runtime is None:
            self._identity_runtime = LicorneActorRuntime(identity_config)
        resolved = await resolve_entities_batch(
            state=self._postgres_state,
            postgres_config=self._config.postgres,
            inference_results=[inference],
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
        self, command: PipelineCommand, step: PipelineStep
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
            postgres_config=self._config.postgres,
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
        self, command: PipelineCommand, step: PipelineStep
    ) -> dict[str, JsonValue]:
        """Runs a scheduled causal slice using actor-local database resources."""
        client, _, graph = await get_pg_stores(
            self._config.postgres, self._postgres_state
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
