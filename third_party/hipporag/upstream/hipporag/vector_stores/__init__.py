from importlib import import_module

_STORE_MODULES = {
    "QdrantEmbeddingStore": ".qdrant_store",
    "ChromaEmbeddingStore": ".chroma_store",
    "MilvusEmbeddingStore": ".milvus_store",
}


def __getattr__(name):
    module_name = _STORE_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    store_class = getattr(import_module(module_name, __name__), name)
    globals()[name] = store_class
    return store_class


__all__ = list(_STORE_MODULES)
