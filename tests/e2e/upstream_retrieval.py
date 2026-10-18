"""Hold inference fixed while comparing upstream graph ranking and an ESKG bridge."""

from __future__ import annotations

import csv
import math
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

import igraph
import numpy as np
from graph_reference import (
    HippoRAGGraphReference,
    compute_mdhash_id,
    min_max_normalize,
    text_processing,
)
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, FiniteFloat
from retrieval_benchmark import Benchmark, Query, recall_at

UPSTREAM_COMMIT = "d5c8329422e0a0b834a15874545cb6a74b4f9b26"


class PhraseVectors(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    profile_id: str
    vectors: dict[str, tuple[FiniteFloat, ...]]


@dataclass(frozen=True, slots=True)
class _Config:
    damping: float = 0.5
    synonymy_edge_topk: int = 2047
    synonymy_edge_sim_threshold: float = 0.8
    synonymy_edge_query_batch_size: int = 1000
    synonymy_edge_key_batch_size: int = 10000


class _Store:
    __slots__ = ("rows", "vectors")

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, str]] = {}
        self.vectors: dict[str, tuple[float, ...]] = {}

    def get_all_id_to_rows(self) -> dict[str, dict[str, str]]:
        return {key: value.copy() for key, value in self.rows.items()}

    def get_row(self, key: str) -> dict[str, str]:
        return self.rows[key]

    def get_embeddings(self, keys: list[str]) -> NDArray[np.float32]:
        return np.asarray([self.vectors[key] for key in keys], dtype=np.float32)


class _Reference(HippoRAGGraphReference):
    """Inject cached stores into unmodified upstream graph methods only."""

    def __init__(self) -> None:
        self.graph = igraph.Graph(directed=False)
        self.global_config = _Config()
        self.entity_embedding_store = _Store()
        self.chunk_embedding_store = _Store()
        self.node_to_node_stats: dict[tuple[str, str], float] = {}
        self.ent_node_to_chunk_ids: dict[str, set[str]] = {}
        self.node_name_to_vertex_idx: dict[str, int] = {}
        self.passage_node_keys: list[str] = []
        self.passage_node_idxs: list[int] = []
        self.passage_embeddings = np.empty((0, 1024), dtype=np.float32)
        self.query_to_embedding: dict[str, dict[str, NDArray[np.float32]]] = {
            "passage": {}
        }
        self.ppr_time = 0.0


def _phrase_vectors(data: Benchmark) -> PhraseVectors:
    cache = PhraseVectors.model_validate_json(
        (
            Path(__file__).parent / "fixtures" / "retrieval_phrases.json"
        ).read_bytes()
    )
    if cache.profile_id != data.embedding_profile:
        raise ValueError("Incompatible phrase embedding profile")
    for vector in cache.vectors.values():
        if len(vector) != 1024 or not math.isclose(
            math.fsum(value * value for value in vector), 1, abs_tol=1e-5
        ):
            raise ValueError("Invalid cached phrase embedding")
    return cache


def _eskg_links(data: Benchmark) -> dict[str, str]:
    with (Path(__file__).parent / "fixtures" / "retail.csv").open() as source:
        rows = {int(row["source_row"]): row for row in csv.DictReader(source)}
    links = {}
    for document in data.documents:
        row = rows.get(document.source_row)
        if (
            row is None
            or int(row["customer_id"]) != document.customer_id
            or row["invoice_date"] != document.invoice_date
        ):
            raise ValueError("Document disagrees with ESKG source identity")
        links[document.id] = f"eskg:retail-customer:{document.customer_id}"
    return links


def rank_upstream(
    data: Benchmark, query: Query, *, allowed: frozenset[str], eskg: bool
) -> list[str]:
    """Apply authorization before constructing any nodes or recognizing facts.

    The ESKG ablation adds source-verified customer identity nodes, without qrels,
    additional query seeds, or access to evidence outside the supplied view.
    """
    documents = [
        document for document in data.documents if document.id in allowed
    ]
    if not documents:
        return []
    if len(documents) > 256:
        raise ValueError("Reference corpus budget exceeded")
    phrases = _phrase_vectors(data)
    reference = _Reference()
    triples = []
    entities = []
    facts: dict[tuple[str, str, str], tuple[float, ...]] = {}
    keys: dict[str, str] = {}
    for document in documents:
        key = compute_mdhash_id(document.text, "chunk-")
        if key in keys:
            raise ValueError("Reference passage content must be unique")
        keys[key] = document.id
        reference.passage_node_keys.append(key)
        reference.chunk_embedding_store.rows[key] = {"content": document.text}
        reference.chunk_embedding_store.vectors[key] = data.vector(
            document.text
        )
        normalized = []
        names: set[str] = set()
        for fact in document.facts:
            triple = (
                text_processing(fact.subject),
                text_processing(fact.predicate),
                text_processing(fact.object),
            )
            normalized.append(triple)
            facts[triple] = data.vector(
                f"{fact.subject} {fact.predicate} {fact.object}"
            )
            names.update((triple[0], triple[2]))
        triples.append(normalized)
        entities.append(sorted(names))
        for name in names:
            entity = compute_mdhash_id(name, "entity-")
            reference.entity_embedding_store.rows[entity] = {"content": name}
            reference.entity_embedding_store.vectors[entity] = phrases.vectors[
                name
            ]
    reference.add_fact_edges(reference.passage_node_keys, triples)
    reference.add_passage_edges(reference.passage_node_keys, entities)
    reference.add_synonymy_edges()
    reference.add_new_nodes()
    reference.add_new_edges()
    if eskg:
        links = _eskg_links(data)
        canonical = sorted({links[document.id] for document in documents})
        reference.graph.add_vertices(canonical)
        reference.graph.add_edges(
            [(key, links[identifier]) for key, identifier in keys.items()],
            attributes={"weight": [1.0] * len(keys)},
        )
    reference.node_name_to_vertex_idx = {
        vertex["name"]: vertex.index for vertex in reference.graph.vs
    }
    reference.passage_node_idxs = [
        reference.node_name_to_vertex_idx[key]
        for key in reference.passage_node_keys
    ]
    reference.passage_embeddings = (
        reference.chunk_embedding_store.get_embeddings(
            reference.passage_node_keys
        )
    )
    reference.query_to_embedding["passage"][query.text] = np.asarray(
        data.vector(query.text, True), dtype=np.float32
    )
    if not facts:
        order, _ = reference.dense_passage_retrieval(query.text)
    else:
        fact_list = list(facts)
        scores = min_max_normalize(
            np.asarray(list(facts.values()), dtype=np.float32)
            @ np.asarray(data.vector(query.text, True), dtype=np.float32)
        )
        candidates = np.argsort(scores)[-5:][::-1].tolist()
        recognized = {
            tuple(
                text_processing(value)
                for value in (fact.subject, fact.predicate, fact.object)
            )
            for fact in data.recognized(query.text)
        }
        selected = [
            index for index in candidates if fact_list[index] in recognized
        ]
        if selected:
            order, _ = reference.graph_search_with_fact_entities(
                query.text,
                5,
                scores,
                [fact_list[index] for index in selected],
                selected,
            )
        else:
            order, _ = reference.dense_passage_retrieval(query.text)
    return [keys[reference.passage_node_keys[int(index)]] for index in order]


class UpstreamReport(TypedDict):
    upstream_commit: str
    scope: str
    queries: int
    eskg_identity_links: int
    recall_at_5: dict[str, float]
    median_seconds: dict[str, float]
    selections: dict[str, dict[str, list[str]]]


def compare_upstream(data: Benchmark) -> UpstreamReport:
    """Report graph-only ablations without asserting an improvement on a tiny corpus."""
    links = _eskg_links(data)
    allowed = frozenset(document.id for document in data.documents)
    recalls: dict[str, float] = {}
    selections: dict[str, dict[str, list[str]]] = {}
    elapsed: dict[str, float] = {}
    for label, enabled in (
        ("upstream_graph", False),
        ("upstream_graph_eskg", True),
    ):
        values = []
        times = []
        selections[label] = {}
        for query in data.queries:
            started = time.perf_counter()
            ranked = rank_upstream(data, query, allowed=allowed, eskg=enabled)
            times.append(time.perf_counter() - started)
            selections[label][query.id] = ranked[:5]
            values.append(recall_at(ranked, set(query.relevant), 5))
        recalls[label] = statistics.mean(values)
        elapsed[label] = statistics.median(times)
    return {
        "upstream_commit": UPSTREAM_COMMIT,
        "scope": "graph-kernel; cached extraction and recognition; exact NumPy cosine KNN",
        "queries": len(data.queries),
        "eskg_identity_links": len(links),
        "recall_at_5": recalls,
        "median_seconds": elapsed,
        "selections": selections,
    }
