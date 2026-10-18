import hashlib
import os
import re


def resolve_vector_store_namespace(global_config=None, db_path: str = None) -> str:
    """Resolve one stable index namespace shared by chunk, entity, and fact stores."""
    configured = getattr(global_config, "vector_store_namespace", None) if global_config is not None else None
    if configured:
        return configured
    if global_config is not None and all(getattr(global_config, name, None) for name in ("save_dir", "llm_name", "embedding_model_name")):
        llm_label = global_config.llm_name.replace("/", "_")
        embedding_label = global_config.embedding_model_name.replace("/", "_")
        return os.path.abspath(os.path.join(global_config.save_dir, f"{llm_label}_{embedding_label}"))
    if db_path is None:
        raise ValueError("db_path is required when vector_store_namespace cannot be resolved from configuration.")
    return os.path.abspath(os.path.dirname(db_path))


def build_collection_name(db_path: str, namespace: str, global_config=None) -> str:
    """Build a backend-safe collection name isolated to one HippoRAG index."""
    index_namespace = resolve_vector_store_namespace(global_config, db_path)
    namespace_hash = hashlib.sha256(index_namespace.encode("utf-8")).hexdigest()[:16]
    safe_store_namespace = re.sub(r"[^0-9A-Za-z_]", "_", namespace).strip("_") or "default"
    return f"hipporag_{namespace_hash}_{safe_store_namespace[:32]}"
