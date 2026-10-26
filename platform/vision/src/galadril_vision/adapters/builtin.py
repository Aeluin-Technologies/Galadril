"""Route-owned built-in implementations of the step execution port."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import cast

import orjson
from galadril_inference.common.types import PredictionRequest
from galadril_pipeline.config import PipelineStep, StepType
from galadril_pipeline.events import PipelineCommand
from pydantic import JsonValue, TypeAdapter

from galadril_vision.adapters.inference import get_inference_engine
from galadril_vision.adapters.memory import MemorySink
from galadril_vision.adapters.routing import (
    ForwardingStepAdapter,
    RoutingStepAdapter,
)
from galadril_vision.application.errors import CommandProcessingError
from galadril_vision.application.execution import StepHandler
from galadril_vision.common.config import VisionConfig
from galadril_vision.compute.helpers import (
    _build_raw_data_record,
    _decode_raw_content,
    _extract_text_payload,
    _infer_modality,
    _is_tenant_raw_key,
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


@dataclass(slots=True)
class IdentityRuntimeState:
    """Preserves one allocator owner across compatible routes of a tenant."""

    runtime: LicorneActorRuntime | None = None


class BuiltinAdapterFactory:
    """Separates connection ownership from tenant identity sequence ownership."""

    __slots__ = ("_identities",)

    def __init__(self) -> None:
        self._identities: dict[
            tuple[bool, str], tuple[VisionConfig, IdentityRuntimeState]
        ] = {}

    def __call__(self, config: VisionConfig) -> Mapping[str, StepHandler]:
        """Fails closed when one tenant is assigned incompatible resolvers."""
        state = IdentityRuntimeState()
        if config.identity_resolution.enabled:
            key = (
                config.runtime_tenant_id is not None,
                config.runtime_tenant_id or config.name,
            )
            existing = self._identities.get(key)
            if existing is None:
                self._identities[key] = (config, state)
            else:
                previous, state = existing
                if (
                    previous.identity_resolution != config.identity_resolution
                    or previous.postgres != config.postgres
                ):
                    raise ValueError(
                        "Tenant identity resolver bindings must agree"
                    )
        adapter = BuiltinStepAdapter(config, identity_state=state)
        return {
            step.step: MemorySink(config)
            if step.capability_id == "core.output.hipporag2"
            else RoutingStepAdapter(step, config.connectors.scribe_endpoint)
            if step.type is StepType.ROUTE
            else ForwardingStepAdapter()
            if step.type is StepType.FORWARD
            else adapter
            for step in config.to_pipeline_config().pipeline
        }


class BuiltinStepAdapter:
    """Keeps backend resources within one trusted published route."""

    __slots__ = (
        "_config",
        "_postgres_state",
        "_s3_client",
        "_identity_state",
    )

    def __init__(
        self,
        config: VisionConfig,
        *,
        identity_state: IdentityRuntimeState | None = None,
    ) -> None:
        """Defers I/O until execution inside the owning Ray worker."""
        self._config = config
        self._postgres_state = PostgresRuntimeState()
        self._s3_client: S3Client | None = None
        self._identity_state = (
            identity_state
            if identity_state is not None
            else IdentityRuntimeState()
        )

    async def execute(
        self,
        command: PipelineCommand,
        step: PipelineStep,
    ) -> dict[str, JsonValue]:
        """Dispatches only after the tenant ontology context has been bound."""
        config = self._config
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
        models_store = config.models_store
        engine = await get_inference_engine(
            model_name=step.model,
            models_bucket=models_store.bucket,
            models_prefix=models_store.prefix,
            endpoint_url=models_store.endpoint_url,
            access_key=models_store.access_key,
            secret_key=models_store.secret_key,
            region_name=models_store.region_name,
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
        if identity_config.enabled and self._identity_state.runtime is None:
            self._identity_state.runtime = LicorneActorRuntime(identity_config)
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
            resolver=self._identity_state.runtime
            if identity_config.enabled
            else None,
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
            ontology_resource_id=_optional_string(
                params, "ontology_resource_id"
            ),
            event_ontology_resource_id=_optional_string(
                params, "event_ontology_resource_id"
            ),
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
        from galadril_vision.causal.runner import (
            AmarthCausalRunner,
            build_slice_spec_from_step_params,
        )

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


def _optional_string(parameters: dict[str, object], name: str) -> str | None:
    """Rejects malformed classifications before reaching the storage boundary."""
    value = parameters.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise CommandProcessingError(f"{name} must be a non-empty resource ID")
    return value


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
    """Requires the canonical tenant-first raw object partition."""
    if not _is_tenant_raw_key(key, tenant_id):
        raise CommandProcessingError(
            "Raw object key is outside the command tenant partition"
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
