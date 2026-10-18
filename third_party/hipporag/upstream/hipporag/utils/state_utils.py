import hashlib
import json
import os
from typing import Any, Dict, Hashable, Iterable, Set

from filelock import FileLock

from ..vector_stores.naming import resolve_vector_store_namespace


class StateConsistencyError(RuntimeError):
    """Raised when persisted index components cannot form one consistent RAG state."""


def embedding_index_identity(config) -> Dict[str, Any]:
    """Return the configuration fields that define one embedding vector space."""
    azure_api_version = None
    azure_deployment = None
    if config.azure_embedding_endpoint is not None:
        azure_api_version = config.azure_embedding_api_version or config.azure_api_version
        azure_deployment = config.azure_embedding_deployment or config.embedding_model_name
    store_type = config.vector_store_type
    endpoint_identity = None
    if store_type == "qdrant" and config.qdrant_url:
        endpoint_identity = (config.qdrant_url,)
    elif store_type == "chroma" and config.chroma_host:
        endpoint_identity = (config.chroma_host, config.chroma_port)
    elif store_type == "milvus":
        milvus_uri = config.milvus_uri or os.getenv("MILVUS_URI")
        milvus_db_name = config.milvus_db_name or os.getenv("MILVUS_DB_NAME")
        if milvus_uri:
            endpoint_identity = (milvus_uri, milvus_db_name)
    endpoint_fingerprint = hashlib.sha256(json.dumps(endpoint_identity, sort_keys=True).encode("utf-8")).hexdigest() if endpoint_identity is not None else None
    namespace_fingerprint = None
    if store_type != "parquet":
        namespace_fingerprint = hashlib.sha256(resolve_vector_store_namespace(config).encode("utf-8")).hexdigest()
    return {
        "provider": config.embedding_provider,
        "model_name": config.embedding_model_name,
        "base_url": config.embedding_base_url,
        "azure_endpoint": config.azure_embedding_endpoint,
        "azure_api_version": azure_api_version,
        "azure_deployment": azure_deployment,
        "normalized": config.embedding_return_as_normalized,
        "max_sequence_length": config.embedding_max_seq_len,
        "dtype": config.embedding_model_dtype,
        "vector_store_type": store_type,
        "vector_store_namespace": config.vector_store_namespace,
        "vector_store_namespace_fingerprint": namespace_fingerprint,
        "vector_store_endpoint_fingerprint": endpoint_fingerprint,
    }


def component_class_identity(component: Any) -> str:
    """Return a stable class-level identity for an index-producing component."""
    component_class = component.__class__
    return f"{component_class.__module__}.{component_class.__qualname__}"


def validate_or_create_index_manifest(
    manifest_path: str,
    manifest: Dict[str, Any],
    stores: Iterable[Any],
    legacy_state_paths: Iterable[str] = (),
    allow_empty_rewrite: bool = False,
) -> None:
    """Atomically bind persisted vectors and graph state to their producing configuration."""
    temporary_path = manifest_path + ".tmp"
    with FileLock(manifest_path + ".lock"):
        stores = tuple(stores)
        legacy_state_paths = tuple(legacy_state_paths)
        has_derived_state = any(store.get_all_ids() for store in stores) or any(os.path.exists(path) for path in legacy_state_paths)
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, encoding="utf-8") as manifest_file:
                    stored_manifest = json.load(manifest_file)
            except (OSError, json.JSONDecodeError) as exc:
                raise StateConsistencyError(f"Cannot read index manifest {manifest_path}: {exc}. Restore it or use a fresh save_dir.") from exc
            if stored_manifest != manifest and not (allow_empty_rewrite and not has_derived_state):
                raise StateConsistencyError(f"Index schema/config mismatch in {manifest_path}. Use a fresh save_dir for a full rebuild.")
            if stored_manifest == manifest:
                return
        elif has_derived_state:
            raise StateConsistencyError("This index predates its schema manifest and cannot be matched to the current embedding configuration safely. Use a fresh save_dir for a full rebuild.")
        try:
            with open(temporary_path, "w", encoding="utf-8") as manifest_file:
                json.dump(manifest, manifest_file, ensure_ascii=False, indent=2)
            os.replace(temporary_path, manifest_path)
        finally:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)


def remove_sources_from_mapping(mapping: Dict[Hashable, Set[str]], key: Hashable, sources_to_remove: Set[str]) -> bool:
    """Remove sources from a reverse mapping and report whether the key became unreferenced."""
    remaining_sources = mapping[key].difference(sources_to_remove)
    if remaining_sources:
        mapping[key] = remaining_sources
        return False
    del mapping[key]
    return True
