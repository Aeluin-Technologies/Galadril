"""Run pinned upstream indexing and retrieval over an already authorized evidence view."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Self

import numpy as np
from hipporag import HippoRAG
from hipporag.utils.config_utils import BaseConfig
from hipporag.utils.misc_utils import (
    Chunk,
    NerRawOutput,
    TripleRawOutput,
    compute_mdhash_id,
    text_processing,
)
from numpy.typing import NDArray
from opentelemetry import trace

from galadril_scribe.retrieval import Fact, Passage

_tracer = trace.get_tracer("galadril.document_memory.official")
# Upstream free-form exceptions can contain evidence; our spans report only bounded metadata.
_upstream_log = logging.getLogger("hipporag")
_upstream_log.addHandler(logging.NullHandler())
_upstream_log.propagate = False
UPSTREAM_COMMIT = "d5c8329422e0a0b834a15874545cb6a74b4f9b26"
Triple = tuple[str, str, str]


def triple(fact: Fact) -> Triple:
    return (
        text_processing(fact.subject),
        text_processing(fact.predicate),
        text_processing(fact.object),
    )


@dataclass(frozen=True, slots=True)
class EvidenceLink:
    passage_id: str
    entity_id: str
    event_id: str


class _Embeddings:
    __slots__ = ("vectors", "query", "query_vector")
    query_instruction_mode = "ignored"

    def __init__(self, vectors: Mapping[str, tuple[float, ...]]) -> None:
        self.vectors = vectors
        self.query = ""
        self.query_vector: tuple[float, ...] = ()

    def batch_encode(
        self, texts: list[str] | str, **kwargs: object
    ) -> NDArray[np.float32]:
        items = [texts] if isinstance(texts, str) else texts
        matrix = np.asarray(
            [
                self.query_vector
                if text == self.query and kwargs.get("norm")
                else self.vectors[text]
                for text in items
            ],
            dtype=np.float32,
        )
        matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
        return matrix


class _LocalOnly:
    __slots__ = ()

    def infer(self, **kwargs: object) -> tuple[str, dict[str, object], bool]:
        raise ValueError("External inference is disabled for document memory")


class _OpenIE:
    __slots__ = ("facts",)

    def __init__(self, passages: tuple[Passage, ...]) -> None:
        self.facts: dict[str, set[Triple]] = {}
        for passage in passages:
            self.facts.setdefault(passage.text, set()).update(
                triple(fact) for fact in passage.facts
            )

    def batch_openie(
        self, rows: dict[str, dict[str, str]]
    ) -> tuple[dict[str, NerRawOutput], dict[str, TripleRawOutput]]:
        entities: dict[str, NerRawOutput] = {}
        facts: dict[str, TripleRawOutput] = {}
        for key, row in rows.items():
            triples = sorted(self.facts[row["content"]])
            endpoints = sorted(
                {value for item in triples for value in (item[0], item[2])}
            )
            entities[key] = NerRawOutput(
                chunk_id=key,
                response="",
                unique_entities=endpoints,
                metadata={"producer": "galadril_grounded_facts"},
            )
            facts[key] = TripleRawOutput(
                chunk_id=key,
                response="",
                triples=[list(item) for item in triples],
                metadata={"producer": "galadril_grounded_facts"},
            )
        return entities, facts


class _Recognition:
    __slots__ = ("selected",)

    def __init__(self, selected: tuple[Fact, ...]) -> None:
        self.selected = frozenset(triple(fact) for fact in selected)

    def __call__(
        self,
        query: str,
        candidate_items: list[Triple],
        candidate_indices: list[int],
        len_after_rerank: int,
    ) -> tuple[list[int], list[Triple], dict[str, object]]:
        pairs = [
            (index, fact)
            for index, fact in zip(
                candidate_indices, candidate_items, strict=True
            )
            if fact in self.selected
        ][:len_after_rerank]
        return (
            [index for index, _ in pairs],
            [fact for _, fact in pairs],
            {"producer": "galadril_recognition"},
        )


class OfficialHippoRAG:
    """Keep upstream graph semantics while retaining Galadril's local inference and ACLs."""

    __slots__ = (
        "_engine",
        "_embeddings",
        "_passages",
        "_links",
        "_extensions",
        "_dimension",
        "used_graph",
    )
    implementation_module = HippoRAG.__module__

    @staticmethod
    def embedding_texts(passages: tuple[Passage, ...]) -> tuple[str, ...]:
        return tuple(OfficialHippoRAG.embedding_inputs(passages))

    @staticmethod
    def embedding_inputs(passages: tuple[Passage, ...]) -> dict[str, str]:
        texts = {passage.text: passage.text for passage in passages}
        facts: dict[str, str] = {}

        def insert(key: str, value: str) -> None:
            previous = texts.get(key)
            if previous is not None and previous != value:
                raise ValueError("Ambiguous embedding serialization")
            texts[key] = value

        for passage in passages:
            for fact in passage.facts:
                subject, predicate, obj = triple(fact)
                insert(subject, subject)
                insert(obj, obj)
                key = str((subject, predicate, obj))
                value = f"{fact.subject} {fact.predicate} {fact.object}"
                facts[key] = min(facts.get(key, value), value)
        for key, value in facts.items():
            insert(key, value)
        return texts

    def __init__(
        self,
        directory: Path,
        passages: tuple[Passage, ...],
        vectors: Mapping[str, tuple[float, ...]],
        *,
        profile: str,
        links: tuple[EvidenceLink, ...] = (),
    ) -> None:
        identifiers = {passage.id for passage in passages}
        if not 1 <= len(passages) <= 256 or len(identifiers) != len(passages):
            raise ValueError("Invalid evidence scope")
        if len(links) > 1024 or any(
            link.passage_id not in identifiers
            or not link.entity_id
            or not link.event_id
            for link in links
        ):
            raise ValueError("ESKG link exceeds evidence scope")
        required = self.embedding_texts(passages)
        dimensions = {len(vectors.get(text, ())) for text in required}
        if len(dimensions) != 1 or not all(
            0 < size <= 4096 for size in dimensions
        ):
            raise ValueError("Missing or incompatible evidence embeddings")
        self._dimension = next(iter(dimensions))
        self.used_graph = False
        for text in required:
            vector = np.asarray(vectors[text], dtype=np.float32)
            if not np.isfinite(vector).all() or np.linalg.norm(vector) <= 1e-12:
                raise ValueError("Invalid evidence embedding")
        if len(profile) != 64 or any(
            char not in "0123456789abcdef" for char in profile
        ):
            raise ValueError("Invalid embedding profile")
        # Never reopen an external pickle or mix an earlier actor's evidence view.
        directory.mkdir(parents=True, exist_ok=True)
        if any(directory.iterdir()):
            raise ValueError(
                "Official engine requires a fresh evidence directory"
            )
        self._passages = {passage.id: passage for passage in passages}
        self._links = links
        self._extensions: list[str] = []
        self._embeddings = _Embeddings(vectors)
        config = BaseConfig(
            save_dir=str(directory),
            llm_name="galadril-local-eru",
            embedding_model_name="galadril-local-qwen",
            save_openie=False,
            linking_top_k=5,
        )
        with _tracer.start_as_current_span(
            "hipporag.index",
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            span.set_attribute("evidence.passages", len(passages))
            span.set_attribute("hipporag.commit", UPSTREAM_COMMIT)
            self._engine = HippoRAG(
                global_config=config,
                embedding_model=self._embeddings,
                extraction_llm=_LocalOnly(),
                index_identity=profile,
            )
            try:
                self._engine.openie = _OpenIE(passages)
                self._engine.index(
                    [
                        Chunk(content=text)
                        for text in dict.fromkeys(
                            passage.text for passage in passages
                        )
                    ]
                )
            except Exception:
                self._engine.close()
                raise

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._engine.close()

    @property
    def identity_link_count(self) -> int:
        return len(self._links)

    def _remove_extensions(self) -> None:
        if self._extensions:
            self._engine.graph.delete_vertices(self._extensions)
            self._extensions.clear()
        self._engine.ready_to_retrieve = False

    def _extend_eskg(self) -> None:
        graph = self._engine.graph
        nodes: set[str] = set()
        edges: set[tuple[str, str]] = set()
        for link in self._links:
            passage = self._passages[link.passage_id]
            chunk = compute_mdhash_id(passage.text, prefix="chunk-")
            entity = (
                "eskg-entity-"
                + hashlib.sha256(link.entity_id.encode()).hexdigest()
            )
            event = (
                "eskg-event-"
                + hashlib.sha256(link.event_id.encode()).hexdigest()
            )
            nodes.update((entity, event))
            edges.update(((chunk, event), (event, entity)))
        self._extensions = sorted(nodes)
        graph.add_vertices(self._extensions)
        graph.add_edges(
            sorted(edges), attributes={"weight": [1.0] * len(edges)}
        )

    def retrieve(
        self,
        query: str,
        query_vector: tuple[float, ...],
        *,
        recognized: tuple[Fact, ...],
    ) -> list[tuple[str, float]]:
        if (
            len(query_vector) != self._dimension
            or not np.isfinite(query_vector).all()
            or np.linalg.norm(query_vector) <= 1e-12
        ):
            raise ValueError("Invalid query embedding")
        self._remove_extensions()
        self._engine.prepare_retrieval_objects()
        self._extend_eskg()
        self._embeddings.query, self._embeddings.query_vector = (
            query,
            query_vector,
        )
        self._engine.rerank_filter = _Recognition(recognized)
        with _tracer.start_as_current_span(
            "hipporag.retrieve",
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            span.set_attribute("eskg.links", len(self._links))
            results = self._engine.retrieve(
                [query], num_to_retrieve=len(self._passages)
            )
        result = results[0]
        self.used_graph = bool(result.graph_seeds)
        aliases: dict[str, list[str]] = {}
        for passage in self._passages.values():
            aliases.setdefault(passage.text, []).append(passage.id)
        return [
            (identifier, float(score))
            for text, score in zip(result.docs, result.doc_scores, strict=True)
            for identifier in sorted(aliases[text])
        ]

    def delete(self, identifiers: tuple[str, ...]) -> None:
        self._remove_extensions()
        removed = {
            self._passages[identifier].text for identifier in identifiers
        }
        for identifier in identifiers:
            del self._passages[identifier]
        retained = {passage.text for passage in self._passages.values()}
        self._engine.delete(sorted(removed - retained))
        self._links = tuple(
            link for link in self._links if link.passage_id in self._passages
        )


class OfficialViewCache:
    """Reuse one exact authorized view; scope changes cannot reuse its graph or stores."""

    __slots__ = ("_engine", "_directory", "_fingerprint", "builds")

    def __init__(self) -> None:
        self._engine: OfficialHippoRAG | None = None
        self._directory: TemporaryDirectory[str] | None = None
        self._fingerprint: bytes | None = None
        self.builds = 0

    def close(self) -> None:
        engine, self._engine = self._engine, None
        directory, self._directory = self._directory, None
        self._fingerprint = None
        try:
            if engine is not None:
                engine.close()
        finally:
            if directory is not None:
                directory.cleanup()

    @property
    def used_graph(self) -> bool:
        return self._engine is not None and self._engine.used_graph

    def rank(
        self,
        passages: tuple[Passage, ...],
        vectors: Mapping[str, tuple[float, ...]],
        *,
        profile: str,
        links: tuple[EvidenceLink, ...],
        query: str,
        query_vector: tuple[float, ...],
        recognized: tuple[Fact, ...],
    ) -> list[tuple[str, float]]:
        digest = hashlib.sha256(b"galadril-official-view-v1")

        def field(value: str) -> None:
            encoded = value.encode()
            digest.update(len(encoded).to_bytes(8, "little"))
            digest.update(encoded)

        field(profile)
        field(str(len(passages)))
        for passage in sorted(passages, key=lambda item: item.id):
            field(passage.id)
            field(passage.text)
            field(str(len(passage.facts)))
            for fact in passage.facts:
                field(fact.model_dump_json())
        field(str(len(links)))
        for link in sorted(
            links,
            key=lambda item: (item.passage_id, item.entity_id, item.event_id),
        ):
            field(link.passage_id)
            field(link.entity_id)
            field(link.event_id)
        fingerprint = digest.digest()
        trace.get_current_span().set_attribute(
            "hipporag.cache_hit", self._fingerprint == fingerprint
        )
        try:
            if self._fingerprint != fingerprint:
                self.close()
                self._directory = TemporaryDirectory(
                    prefix="galadril-hipporag-view-"
                )
                self._engine = OfficialHippoRAG(
                    Path(self._directory.name),
                    passages,
                    vectors,
                    profile=profile,
                    links=links,
                )
                self._fingerprint = fingerprint
                self.builds += 1
            if self._engine is None:
                raise ValueError("Official view is unavailable")
            return self._engine.retrieve(
                query, query_vector, recognized=recognized
            )
        except Exception:
            self.close()
            raise
