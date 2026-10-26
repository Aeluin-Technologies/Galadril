"""Cache phrase embeddings once for the pinned upstream graph-kernel reference."""

import argparse
import asyncio
import json
from pathlib import Path

from galadril_scribe.retrieval_contracts import EmbedTexts
from galadril_scribe.retrieval_models import (
    LegacyTextEmbeddings as LocalEmbeddings,
)
from retrieval_benchmark import load_benchmark


def normalize(text: str) -> str:
    return " ".join(
        "".join(
            character if character.isalnum() or character.isspace() else " "
            for character in text.casefold()
        ).split()
    )


async def generate(output: Path, cache: str) -> None:
    source = load_benchmark()
    phrases = sorted(
        {
            normalize(value)
            for document in source.documents
            for fact in document.facts
            for value in (fact.subject, fact.object)
        }
    )
    model = LocalEmbeddings(cache)
    vectors: dict[str, tuple[float, ...]] = {}
    for offset in range(0, len(phrases), 32):
        batch = tuple(phrases[offset : offset + 32])
        embedded = await model.embed(EmbedTexts(texts=batch, query=False))
        if embedded.profile_id != source.embedding_profile:
            raise ValueError("Phrase embedding profile changed")
        vectors.update(zip(batch, embedded.vectors, strict=True))
    output.write_text(
        json.dumps(
            {"profile_id": source.embedding_profile, "vectors": vectors},
            separators=(",", ":"),
        )
        + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", required=True)
    arguments = parser.parse_args()
    asyncio.run(generate(arguments.output, arguments.cache))
