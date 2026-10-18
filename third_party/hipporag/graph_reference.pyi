"""Typed benchmark boundary for the pinned upstream graph methods."""

import numpy as np
from numpy.typing import NDArray

def text_processing(text: str) -> str: ...
def compute_mdhash_id(content: str, prefix: str = "") -> str: ...
def min_max_normalize(x: NDArray[np.float32]) -> NDArray[np.float32]: ...

class HippoRAGGraphReference:
    def add_fact_edges(
        self,
        chunk_ids: list[str],
        chunk_triples: list[list[tuple[str, str, str]]],
    ) -> None: ...
    def add_passage_edges(
        self, chunk_ids: list[str], chunk_triple_entities: list[list[str]]
    ) -> int: ...
    def add_synonymy_edges(
        self, query_node_keys: list[str] | None = None
    ) -> None: ...
    def add_new_nodes(self) -> None: ...
    def add_new_edges(self) -> None: ...
    def dense_passage_retrieval(
        self, query: str
    ) -> tuple[NDArray[np.int64], NDArray[np.float32]]: ...
    def graph_search_with_fact_entities(
        self,
        query: str,
        link_top_k: int,
        query_fact_scores: NDArray[np.float32],
        top_k_facts: list[tuple[str, ...]],
        top_k_fact_indices: list[int],
        passage_node_weight: float = 0.05,
    ) -> tuple[NDArray[np.int64], NDArray[np.float32]]: ...
