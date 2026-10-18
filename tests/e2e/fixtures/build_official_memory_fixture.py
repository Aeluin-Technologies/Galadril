"""Cache local query embeddings and reranking once for a fixed, source-derived protocol."""

import argparse
import asyncio
from pathlib import Path

from galadril_scribe.retrieval_contracts import EmbedTexts, RecognizeFacts
from galadril_scribe.retrieval_eru import anchored_indices
from galadril_scribe.retrieval_models import LocalEmbeddings, LocalReranker
from official_memory import MemoryFixture, MemoryQuestion
from retrieval_benchmark import cosine, load_benchmark


async def generate(cache: Path, output: Path) -> None:
    data = load_benchmark()
    embedder = LocalEmbeddings(str(cache / "qwen"))
    reranker = LocalReranker(str(cache / "bge"))
    groups = sorted({document.customer_id for document in data.documents})
    questions: list[MemoryQuestion] = []
    for customer in groups:
        documents = [
            document
            for document in data.documents
            if document.customer_id == customer
        ]
        product = next(
            document
            for document in documents
            if document.id.startswith("source-row:")
        )
        invoice = next(
            document
            for document in documents
            if document.id.startswith("invoice:")
        )
        country = next(
            document
            for document in documents
            if document.id.startswith("customer:")
        )
        cases = (
            (
                f"Which country is connected to {product.facts[0].subject} through its invoice and customer?",
                (product.id, invoice.id, country.id),
            ),
            (
                f"Which product is linked to Customer {customer}, and on which invoice?",
                (product.id, invoice.id),
            ),
            (
                f"What was the transaction date of {invoice.facts[0].subject}, and which country does its customer trade with?",
                (invoice.id, country.id),
            ),
            (
                f"Which product is connected to {invoice.facts[0].subject}, and what is its unit price?",
                (product.id,),
            ),
        )
        for number, (text, relevant) in enumerate(cases):
            embedded = await embedder.embed(
                EmbedTexts(texts=(text,), query=True)
            )
            if embedded.profile_id != data.embedding_profile:
                raise ValueError("Embedding model profile changed")
            scores = await reranker.scores(
                text, tuple(document.text for document in data.documents)
            )
            if reranker.profile_id != data.reranker_profile:
                raise ValueError("Reranker model profile changed")
            facts = tuple(
                {
                    "passage_id": "a" * 64,
                    "ordinal": 0,
                    "fact": fact,
                    "similarity": cosine(
                        embedded.vectors[0],
                        data.vector(
                            f"{fact.subject} {fact.predicate} {fact.object}"
                        ),
                    ),
                }
                for document in data.documents
                for fact in document.facts
            )
            request = RecognizeFacts(query=text, facts=facts)
            recognized = tuple(
                request.facts[index].fact for index in anchored_indices(request)
            )
            questions.append(
                MemoryQuestion(
                    id=f"{product.source_row}:{number}",
                    text=text,
                    source_row=product.source_row,
                    relevant=relevant,
                    query_vector=embedded.vectors[0],
                    reranking=dict(
                        zip(
                            (document.id for document in data.documents),
                            scores,
                            strict=True,
                        )
                    ),
                    recognized=recognized,
                )
            )
            print(
                f"Cached {len(questions)} / {len(groups) * 4} local questions",
                flush=True,
            )
    fixture = MemoryFixture(
        source_sha256=data.source_sha256,
        embedding_profile=data.embedding_profile,
        reranker_profile=data.reranker_profile,
        recognition="local-eru:anchored_indices-v1",
        questions=tuple(questions),
    )
    output.write_text(fixture.model_dump_json() + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(generate(args.cache, args.output))
