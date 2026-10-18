import os
from typing import List, Tuple
import sqlite3
import json
import time
import hashlib

import litellm
from filelock import FileLock

from .base import BaseLLM, LLMConfig, normalize_generation_token_params
from ..utils.llm_utils import TextChatMessage
from ..utils.logging_utils import get_logger


logger = get_logger(__name__)


class LLM_Cache:
    def __init__(self, cache_dir: str, cache_filename, cache_identity=None):
        os.makedirs(cache_dir, exist_ok=True)
        self.cache_filepath =  os.path.join(cache_dir, f"{cache_filename}.sqlite")
        self.lock_file = self.cache_filepath + ".lock"
        self.cache_identity = cache_identity

        self.__db_operation("""
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                message TEXT,
                metadata TEXT
            )
        """, commit=True)
    
    def __db_operation(self, sql, parameters=(), commit=False, fetchone=False):
        with FileLock(self.lock_file):
            conn = sqlite3.connect(self.cache_filepath)
            c = conn.cursor()
            c.execute(sql, parameters)
            if commit:
                conn.commit()
            if fetchone:
                row = c.fetchone()
            conn.close()
            if fetchone:
                return row

    def __params_to_key(self, params):
        key_str = json.dumps({"schema": 3, "identity": self.cache_identity, "params": params}, sort_keys=True, default=str)
        return hashlib.sha256(key_str.encode("utf-8")).hexdigest()

    def key_lock(self, params):
        key = self.__params_to_key(params)
        return FileLock(f"{self.cache_filepath}.{key[:16]}.key.lock")

    def read(self, params):
        key = self.__params_to_key(params)
        row = self.__db_operation("SELECT message, metadata FROM cache WHERE key = ?", (key,), fetchone=True)
        if row is None:
            return None
        message, metadata_str = row
        metadata = json.loads(metadata_str)
        return message, metadata

    def write(self, params, message, metadata):
        key = self.__params_to_key(params)
        metadata_str = json.dumps(metadata)
        self.__db_operation("INSERT OR REPLACE INTO cache (key, message, metadata) VALUES (?, ?, ?)", (key, message, metadata_str), commit=True)


class BedrockLLM(BaseLLM):
    """
    To select this implementation you can initialise HippoRAG with:
        llm_model_name="anthropic.claude-3-5-haiku-20241022-v1:0" or any other Bedrock Model-ID
    """
    def __init__(self, global_config = None):
        self.global_config = global_config
        super().__init__(global_config)
        self._init_llm_config()

        self.cache = LLM_Cache(
            os.path.join(global_config.save_dir, "llm_cache"),
            self.llm_name.replace('/', '_'),
            cache_identity={
                "provider": f"{self.__class__.__module__}.{self.__class__.__qualname__}",
                "region": global_config.bedrock_region or os.getenv("AWS_REGION_NAME") or os.getenv("AWS_DEFAULT_REGION"),
            },
        )
        
        self.retry = self.global_config.max_retry_attempts
        
        logger.info(f"[BedrockLLM] Model-ID: {self.global_config.llm_name}, Cache: {self.cache.cache_filepath}")

    def _init_llm_config(self) -> None:
        generate_params = {
            "model": self.global_config.llm_name,
            "n": 1,
            "temperature": self.global_config.temperature,
            "max_tokens": self.global_config.max_new_tokens,
        }
        if self.global_config.bedrock_region:
            generate_params["aws_region_name"] = self.global_config.bedrock_region
        config_dict = {
            'llm_name': self.global_config.llm_name,
            'generate_params': generate_params,
        }

        self.llm_config = LLMConfig.from_dict(config_dict=config_dict)
        logger.debug(f"[BedrockLLM] Generation params: {self.llm_config.generate_params}")

    def __llm_call(self, params):
        num, wait_s = 0, 0.5
        while True:
            try:
                return litellm.completion(**params)
            except (litellm.RateLimitError, litellm.Timeout, litellm.APIConnectionError,
                    litellm.ServiceUnavailableError, litellm.InternalServerError) as e:
                num += 1
                if num > self.retry:
                    raise e
                
                logger.warning(f"Bedrock LLM Exception: {e}\nRetry #{num} after {wait_s} seconds")
                time.sleep(wait_s)
                wait_s *= 2

    @staticmethod
    def _parse_response(response) -> Tuple[str, dict]:
        choices = getattr(response, "choices", None)
        if choices is None or len(choices) != 1:
            choice_count = 0 if choices is None else len(choices)
            raise ValueError(f"HippoRAG expected exactly one Bedrock choice, received {choice_count}.")
        choice = choices[0]
        choice_message = getattr(choice, "message", None)
        message = getattr(choice_message, "content", None)
        if not isinstance(message, str) or not message:
            raise ValueError("Bedrock response did not contain non-empty text.")
        usage = getattr(response, "usage", None)
        if usage is None:
            raise ValueError("Bedrock response omitted usage; HippoRAG cannot account for this request safely.")
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        for field_name, value in (("prompt_tokens", prompt_tokens), ("completion_tokens", completion_tokens)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"Bedrock response usage.{field_name} must be a non-negative integer.")
        total_tokens = getattr(usage, "total_tokens", None)
        if total_tokens is None:
            total_tokens = prompt_tokens + completion_tokens
        elif not isinstance(total_tokens, int) or isinstance(total_tokens, bool) or total_tokens < 0:
            raise ValueError("Bedrock response usage.total_tokens must be a non-negative integer when provided.")
        metadata = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "finish_reason": getattr(choice, "finish_reason", None),
        }
        for key, value in (("response_id", getattr(response, "id", None)), ("model", getattr(response, "model", None)), ("request_id", getattr(response, "_request_id", None))):
            if value is not None:
                metadata[key] = value
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        completion_details = getattr(usage, "completion_tokens_details", None)
        cached_tokens = prompt_details.get("cached_tokens") if isinstance(prompt_details, dict) else getattr(prompt_details, "cached_tokens", None)
        reasoning_tokens = completion_details.get("reasoning_tokens") if isinstance(completion_details, dict) else getattr(completion_details, "reasoning_tokens", None)
        if cached_tokens is not None:
            metadata["cached_tokens"] = cached_tokens
        if reasoning_tokens is not None:
            metadata["reasoning_tokens"] = reasoning_tokens
        return message, metadata
    
    def infer(self, messages: List[TextChatMessage], **kwargs) -> Tuple[str, dict, bool]:
        params = normalize_generation_token_params(self.llm_config.generate_params, kwargs, "max_tokens")
        params["messages"] = messages
        choice_count = params.get("n", 1)
        if not isinstance(choice_count, int) or isinstance(choice_count, bool) or choice_count != 1:
            raise ValueError("BedrockLLM supports exactly one response choice per inference call.")
        if params.get("stream") not in (None, False):
            raise ValueError("BedrockLLM does not support streaming responses.")

        with self.cache.key_lock(params):
            cache_lookup = self.cache.read(params)
            if cache_lookup is not None:
                message, metadata = cache_lookup
                return message, metadata, True
            response = self.__llm_call(params)
            message, metadata = self._parse_response(response)
            self.cache.write(params, message, metadata)
            return message, metadata, False
