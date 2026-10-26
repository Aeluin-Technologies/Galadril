"""Cache local model outputs for a label-independent, fixed HotpotQA smoke sample."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Literal

from galadril_scribe.retrieval_contracts import EmbedTexts
from galadril_scribe.retrieval_models import (
    LegacyTextEmbeddings as LocalEmbeddings,
)
from galadril_scribe.retrieval_models import LocalReranker
from hotpot_benchmark import HotpotCache, HotpotQuestion
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

_COMMIT = "d5c8329422e0a0b834a15874545cb6a74b4f9b26"
_SOURCE = f"https://github.com/OSU-NLP-Group/HippoRAG/blob/{_COMMIT}/reproduce/dataset/hotpotqa.json"


class SourceQuestion(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(alias="_id")
    question: str
    kind: Literal["bridge", "comparison"] = Field(alias="type")
    level: str
    context: tuple[tuple[str, tuple[str, ...]], ...]
    supporting_facts: tuple[tuple[str, int], ...]


async def generate(source: Path, output: Path, cache: str, stage: str) -> None:
    if stage == "embed":
        content = source.read_bytes()
        rows = TypeAdapter(list[SourceQuestion]).validate_json(content)
        questions: list[HotpotQuestion] = []
        for kind in ("bridge", "comparison"):
            selected = sorted(
                (
                    row
                    for row in rows
                    if row.kind == kind
                    and row.level == "hard"
                    and len(row.context) == 10
                ),
                key=lambda row: hashlib.sha256(row.id.encode()).hexdigest(),
            )[:20]
            questions.extend(
                HotpotQuestion(
                    id=row.id,
                    question=row.question,
                    kind=row.kind,
                    context=dict(row.context),
                    supporting_facts=row.supporting_facts,
                )
                for row in selected
            )
        if len(questions) != 40:
            raise ValueError("Incomplete HotpotQA strata")
        model = LocalEmbeddings(cache)
        previous = (
            HotpotCache.model_validate_json(output.read_bytes())
            if output.exists()
            else None
        )
        if (
            previous is not None
            and previous.source_sha256 != hashlib.sha256(content).hexdigest()
        ):
            raise ValueError("Cached HotpotQA source changed")
        vectors: dict[str, tuple[float, ...]] = (
            previous.vectors.copy() if previous is not None else {}
        )
        profile = previous.embedding_profile if previous is not None else ""
        for query in (False, True):
            texts = (
                list(dict.fromkeys(question.question for question in questions))
                if query
                else list(
                    dict.fromkeys(
                        text
                        for question in questions
                        for text in question.texts().values()
                    )
                )
            )
            texts = [
                text
                for text in texts
                if hashlib.sha256(f"{int(query)}:{text}".encode()).hexdigest()
                not in vectors
            ]
            for offset in range(0, len(texts), 16):
                batch = tuple(texts[offset : offset + 16])
                embedded = await model.embed(
                    EmbedTexts(texts=batch, query=query)
                )
                if profile and embedded.profile_id != profile:
                    raise ValueError("HotpotQA embedding profile changed")
                profile = embedded.profile_id
                vectors.update(
                    (
                        hashlib.sha256(
                            f"{int(query)}:{text}".encode()
                        ).hexdigest(),
                        vector,
                    )
                    for text, vector in zip(
                        batch, embedded.vectors, strict=True
                    )
                )
        subset = hashlib.sha256(
            json.dumps(
                [item.model_dump() for item in questions],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        result = HotpotCache(
            source_url=_SOURCE,
            source_sha256=hashlib.sha256(content).hexdigest(),
            subset_sha256=subset,
            license="CC-BY-SA-4.0",
            selection="Twenty hard bridge and twenty hard comparison questions with ten contexts; ascending SHA256(question_id); no quality-based exclusions.",
            questions=tuple(questions),
            embedding_profile=profile,
            vectors=vectors,
            reranker_profile=previous.reranker_profile
            if previous is not None
            else "",
            reranking=previous.reranking if previous is not None else {},
        )
        output.write_text(result.model_dump_json() + "\n")
        print(f"Cached {len(vectors)} native HotpotQA embeddings", flush=True)
    else:
        data = HotpotCache.model_validate_json(output.read_bytes())
        reranker = LocalReranker(cache, backend="bge")
        scores = data.reranking.copy()
        reranker_profile = data.reranker_profile
        for question in data.questions:
            if question.id in scores:
                continue
            candidate_texts = question.texts()
            values = await reranker.scores(
                question.question, tuple(candidate_texts.values())
            )
            if reranker_profile and reranker_profile != reranker.profile_id:
                raise ValueError("HotpotQA reranker profile changed")
            reranker_profile = reranker.profile_id
            scores[question.id] = dict(
                zip(candidate_texts, values, strict=True)
            )
        data = data.model_copy(
            update={
                "reranker_profile": reranker_profile,
                "reranking": scores,
            }
        )
        output.write_text(data.model_dump_json() + "\n")
        print(f"Cached {len(data.questions)} HotpotQA rerankings", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--stage", choices=("embed", "rerank"), required=True)
    arguments = parser.parse_args()
    asyncio.run(
        generate(
            arguments.source, arguments.output, arguments.cache, arguments.stage
        )
    )
