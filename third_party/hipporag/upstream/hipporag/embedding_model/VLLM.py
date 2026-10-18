from typing import List
import numpy as np
from tqdm import tqdm

from .base import BaseEmbeddingModel
from ..utils.config_utils import BaseConfig
from ..prompts.linking import get_query_instruction
import requests

class VLLMEmbeddingModel(BaseEmbeddingModel):
    """
    To select this implementation you can initialise HippoRAG with:
        embedding_model_name starts with "VLLM/"
    The embedding base url should contain the v1/embeddings.
    """
    def __init__(self, global_config:BaseConfig, embedding_model_name:str) -> None:
        super().__init__(global_config=global_config)

        self.model_id = embedding_model_name.removeprefix("VLLM/")
        self.embedding_type = 'float'
        self.batch_size = global_config.embedding_batch_size

        self.base_url = global_config.embedding_base_url
        if not self.base_url:
            raise ValueError("embedding_base_url is required for VLLM embedding models")

        self.search_query_instr = set([
            get_query_instruction('query_to_fact'),
            get_query_instruction('query_to_passage')
        ])

    def call_model(self, input_text) -> List[np.ndarray]:
        if isinstance(input_text, str):
            input_text = [input_text]
        headers = {
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": self.model_id,
            "input": input_text,
        }

        response = requests.post(self.base_url, headers=headers, json=payload, timeout=self.global_config.embedding_request_timeout)
        response.raise_for_status()
        result = response.json()
        response_rows = sorted(result["data"], key=lambda row: row.get("index", 0))
        return np.array([row["embedding"] for row in response_rows])

    def encode(self, texts: List[str]) -> np.array:
        response = self.call_model(texts)
        return response

    def batch_encode(self, texts: List[str], **kwargs) -> None:
        if isinstance(texts, str):
            texts = [texts]
        if len(texts) < self.batch_size:
            results = self.encode(texts)
        else:
            results = []
            batch_indexes = list(range(0, len(texts), self.batch_size))
            for i in tqdm(batch_indexes, desc="Batch Encoding"):
                results.append(self.encode(texts[i:i + self.batch_size]))
            results = np.concatenate(results)
        return self._normalize_embeddings(results)
