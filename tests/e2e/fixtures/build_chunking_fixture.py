"""Regenerate bounded native chunking measurements; never run during ordinary tests."""

import argparse
import asyncio
import bisect
from pathlib import Path
from typing import Protocol, cast

from chunking_benchmark import CachedChunk, ChunkingCache, ChunkingVariant
from galadril_inference.common.types import PredictionRequest
from galadril_inference.models.embedding.qwen3 import Qwen3EmbeddingModel
from galadril_scribe.retrieval import structural_chunks
from galadril_scribe.retrieval_contracts import EmbedTexts
from galadril_scribe.retrieval_models import LocalEmbeddings
from pydantic import BaseModel, ConfigDict, FiniteFloat
from retrieval_benchmark import load_benchmark


class Tokenizer(Protocol):
    def tokenize(self, text: bytes) -> list[int]: ...
    def detokenize(self, tokens: list[int]) -> bytes: ...


class SpanOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    profile_id: str
    embeddings: tuple[tuple[FiniteFloat, ...], ...]


class QueryOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    embedding: tuple[FiniteFloat, ...]


async def generate(output: Path, model_cache: str, stage: str) -> None:
    source = load_benchmark()
    if stage == "pooled":
        text = ""
        spans = {}
        for document in source.documents:
            start = len(text)
            text += document.text
            spans[document.id] = (start, len(text))
            text += "\n\n"
        boundaries = {
            "fixed": [
                (start, min(start + 256, len(text)))
                for start in range(0, len(text), 224)
            ],
            "structural": [
                (part.start, part.end)
                for part in structural_chunks(
                    text, max_characters=256, overlap=32
                )
            ],
        }
        embedder = LocalEmbeddings(model_cache)
        variants = {}
        for label, parts in boundaries.items():
            embedded = await embedder.embed(
                EmbedTexts(
                    texts=tuple(text[start:end] for start, end in parts),
                    query=False,
                )
            )
            variants[label] = ChunkingVariant(
                profile=embedded.profile_id,
                chunks=tuple(
                    CachedChunk(
                        text=text[start:end],
                        start=start,
                        end=end,
                        vector=vector,
                    )
                    for (start, end), vector in zip(
                        parts, embedded.vectors, strict=True
                    )
                ),
                query_vectors={
                    query.id: source.vector(query.text, True)
                    for query in source.queries
                },
            )
        data = ChunkingCache(
            source_sha256=source.source_sha256,
            text=text,
            evidence_spans=spans,
            relevant={query.id: query.relevant for query in source.queries},
            variants=variants,
        )
    else:
        data = ChunkingCache.model_validate_json(output.read_bytes())
        model = Qwen3EmbeddingModel()
        model.load(
            model_cache,
            model_tier="0.6b",
            compute_type="q8_0",
            n_gpu_layers=0,
            n_ctx=8192,
            embedding_mode="tokens",
        )
        tokenizer = cast(Tokenizer, model._ctx)
        tokens = tokenizer.tokenize(data.text.encode())
        ends = [
            len(tokenizer.detokenize(tokens[:index]))
            for index in range(len(tokens) + 1)
        ]
        contextual_parts = data.variants["structural"].chunks
        token_spans = []
        for chunk in contextual_parts:
            start = len(data.text[: chunk.start].encode())
            end = len(data.text[: chunk.end].encode())
            token_spans.append(
                (
                    max(0, bisect.bisect_right(ends, start) - 1),
                    min(len(tokens), bisect.bisect_left(ends, end)),
                )
            )
        contextual_output = SpanOutput.model_validate(
            model.predict(
                PredictionRequest(
                    model_name="qwen_embedding",
                    features={
                        "text": data.text,
                        "token_spans": token_spans,
                        "dimensions": 1024,
                    },
                )
            ).prediction
        )
        queries = {
            query.id: QueryOutput.model_validate(
                model.predict(
                    PredictionRequest(
                        model_name="qwen_embedding",
                        features={"text": query.text, "dimensions": 1024},
                    )
                ).prediction
            ).embedding
            for query in source.queries
        }
        variant = ChunkingVariant(
            profile=contextual_output.profile_id,
            chunks=tuple(
                chunk.model_copy(update={"vector": vector})
                for chunk, vector in zip(
                    contextual_parts, contextual_output.embeddings, strict=True
                )
            ),
            query_vectors=queries,
        )
        data = data.model_copy(
            update={
                "variants": {**data.variants, "causal_contextual_mean": variant}
            }
        )
    output.write_text(data.model_dump_json() + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument(
        "--stage", choices=("pooled", "contextual"), required=True
    )
    arguments = parser.parse_args()
    asyncio.run(generate(arguments.output, arguments.cache, arguments.stage))
