import os
from typing import List, Tuple
import sqlite3
import json
import time
import hashlib
import torch

from transformers import AutoModelForCausalLM, AutoTokenizer
from filelock import FileLock

from .base import BaseLLM, LLMConfig, normalize_generation_token_params
from ..utils.llm_utils import TextChatMessage
from ..utils.logging_utils import get_logger

def convert_text_chat_messages_to_input_ids(messages: List[TextChatMessage], tokenizer, add_assistant_header=True) -> torch.Tensor:
    prompt = tokenizer.apply_chat_template(
        conversation=messages,
        chat_template=None,
        tokenize=False,
        add_generation_prompt=True,
        continue_final_message=False,
        tools=None,
        documents=None,
    )
    input_ids = tokenizer.encode(prompt, return_tensors="pt")
    return input_ids


logger = get_logger(__name__)


class LLM_Cache:
    def __init__(self, cache_dir: str, cache_filename):
        os.makedirs(cache_dir, exist_ok=True)
        self.cache_filepath =  os.path.join(cache_dir, f"{cache_filename}.sqlite")
        self.lock_file = self.cache_filepath + ".lock"

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
        key_params = {key: value for key, value in params.items() if key != "prompt_text"}
        key_params["_cache_schema"] = 2
        key_str = json.dumps(key_params, sort_keys=True, default=str)
        return hashlib.sha256(key_str.encode("utf-8")).hexdigest()

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


class TransformersLLM(BaseLLM):
    """
    To select this implementation you can initialise HippoRAG with:
        llm_model_name="meta-llama/Llama-3.1-8B-Instruct" or any other Transformer Model-ID
    """
    def __init__(self, global_config = None):
        self.global_config = global_config
        super().__init__(global_config)
        self._init_llm_config()

        self.cache = LLM_Cache(
            os.path.join(global_config.save_dir, "llm_cache"),
            self.llm_name.replace('/', '_'))
        self.model_id = self.global_config.llm_name.removeprefix("Transformers/")
        self.model = AutoModelForCausalLM.from_pretrained(self.model_id, device_map="auto", torch_dtype=torch.bfloat16)
        self.tokenizer = AutoTokenizer.from_pretrained(self.model_id)

        self.retry = 5
        
        logger.info(f"[TransformersLLM] Model-ID: {self.global_config.llm_name}, Cache: {self.cache.cache_filepath}")

    def _init_llm_config(self) -> None:
        config_dict = dict(self.global_config.__dict__)
        config_dict['llm_name'] = self.global_config.llm_name.removeprefix("Transformers/")
        config_dict['generate_params'] = {
            "temperature": config_dict.get("temperature", 0.0),
            "max_new_tokens": config_dict.get("max_new_tokens", 2048),
        }

        self.llm_config = LLMConfig.from_dict(config_dict=config_dict)
        logger.info(f"[TransformersLLM] Config: {self.llm_config}")

    def __llm_call(self, params):
        inputs = params["prompt_text"].to(self.model.device)
        max_new_tokens = params.get("max_new_tokens", params.get("max_tokens", 2048))
        temperature = float(params.get("temperature", 0.0))
        generation_params = {"max_new_tokens": max_new_tokens, "do_sample": temperature > 0}
        if temperature > 0:
            generation_params["temperature"] = temperature
        return self.model.generate(inputs, **generation_params)
    
    def infer(self, messages: List[TextChatMessage], **kwargs) -> Tuple[str, dict, bool]:
        params = normalize_generation_token_params(self.llm_config.generate_params, kwargs, "max_new_tokens")
        params["model"] = self.model_id
        params["messages"] = messages
        params["prompt_text"] = convert_text_chat_messages_to_input_ids(messages, self.tokenizer)
        
        cache_lookup = self.cache.read(params)
        if cache_lookup is not None:
            cached = True
            message, metadata = cache_lookup
        else:
            cached = False
            response = self.__llm_call(params)
            prompt_tokens = params["prompt_text"].shape[1]
            if response.ndim != 2 or response.shape[1] < prompt_tokens:
                raise ValueError(f"Unexpected causal LM output shape {tuple(response.shape)} for prompt length {prompt_tokens}.")
            generated_tokens = response[:, prompt_tokens:]
            message = self.tokenizer.decode(generated_tokens[0], skip_special_tokens=True)
            metadata = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": generated_tokens.shape[1],
            }
            self.cache.write(params, message, metadata)

        return message, metadata, cached
