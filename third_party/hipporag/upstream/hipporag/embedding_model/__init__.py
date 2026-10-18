from importlib import import_module

from .base import BaseEmbeddingModel, EmbeddingConfig
from ..utils.logging_utils import get_logger

logger = get_logger(__name__)

_MODEL_MODULES = {
    "ContrieverModel": ".Contriever",
    "GritLMEmbeddingModel": ".GritLM",
    "NVEmbedV2EmbeddingModel": ".NVEmbedV2",
    "OpenAIEmbeddingModel": ".OpenAI",
    "CohereEmbeddingModel": ".Cohere",
    "TransformersEmbeddingModel": ".Transformers",
    "VLLMEmbeddingModel": ".VLLM",
}

_PROVIDER_CLASSES = {
    "openai": "OpenAIEmbeddingModel",
    "transformers": "TransformersEmbeddingModel",
    "vllm": "VLLMEmbeddingModel",
    "gritlm": "GritLMEmbeddingModel",
    "nvembed": "NVEmbedV2EmbeddingModel",
    "contriever": "ContrieverModel",
    "cohere": "CohereEmbeddingModel",
}


def __getattr__(name):
    module_name = _MODEL_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    model_class = getattr(import_module(module_name, __name__), name)
    globals()[name] = model_class
    return model_class


def _get_embedding_model_class(embedding_model_name: str = "nvidia/NV-Embed-v2", provider: str = None):
    if provider is not None:
        class_name = _PROVIDER_CLASSES.get(provider)
        if class_name is None:
            raise ValueError(f"Unknown embedding provider: {provider}")
    elif embedding_model_name.startswith("Transformers/"):
        class_name = "TransformersEmbeddingModel"
    elif embedding_model_name.startswith("VLLM/"):
        class_name = "VLLMEmbeddingModel"
    elif "GritLM" in embedding_model_name:
        class_name = "GritLMEmbeddingModel"
    elif "NV-Embed-v2" in embedding_model_name:
        class_name = "NVEmbedV2EmbeddingModel"
    elif "contriever" in embedding_model_name:
        class_name = "ContrieverModel"
    elif "text-embedding" in embedding_model_name:
        class_name = "OpenAIEmbeddingModel"
    elif "cohere" in embedding_model_name:
        class_name = "CohereEmbeddingModel"
    else:
        # Fallback to OpenAIEmbeddingModel for custom OpenAI-compatible/vLLM endpoints
        logger.info(
            f"Embedding model '{embedding_model_name}' not matched with local HF models. "
            f"Defaulting to OpenAIEmbeddingModel."
        )
        class_name = "OpenAIEmbeddingModel" 
    return __getattr__(class_name)


__all__ = ["BaseEmbeddingModel", "EmbeddingConfig", "_get_embedding_model_class", *_MODEL_MODULES]
