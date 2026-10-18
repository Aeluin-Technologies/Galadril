from ..utils.logging_utils import get_logger
from ..utils.config_utils import BaseConfig

from .openai_gpt import CacheOpenAI
from .base import BaseLLM
from .gateways import GATEWAYS, GatewaySpec, find_gateway


logger = get_logger(__name__)


def _get_llm_class(config: BaseConfig):
    gateway = find_gateway(config.llm_name)
    if gateway is not None:
        from .gateway_llm import OpenAICompatibleGatewayLLM
        return OpenAICompatibleGatewayLLM(config, gateway)

    if config.llm_name.startswith('bedrock-mantle/'):
        from .bedrock_mantle import BedrockMantleLLM
        return BedrockMantleLLM(config)

    if config.llm_name.startswith('bedrock/'):
        from .bedrock_llm import BedrockLLM
        return BedrockLLM(config)

    if config.llm_name.startswith('Transformers/'):
        from .transformers_llm import TransformersLLM
        return TransformersLLM(config)

    return CacheOpenAI.from_experiment_config(config)
