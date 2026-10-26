"""Generate expensive model outputs once; normal tests never load model weights."""

import argparse
import asyncio
import csv
import hashlib
import json
from pathlib import Path

from galadril_scribe.retrieval_contracts import EmbedTexts
from galadril_scribe.retrieval_models import (
    LegacyTextEmbeddings as LocalEmbeddings,
)
from galadril_scribe.retrieval_models import LocalReranker


async def generate(cache: str) -> None:
    root = Path(__file__).parent
    source = root / "retail.csv"
    with source.open() as stream:
        rows = list(csv.DictReader(stream))
    chosen = []
    seen = set()
    for row in rows:
        customer = row["customer_id"]
        if customer not in seen and not row["invoice_no"].startswith("C"):
            chosen.append(row)
            seen.add(customer)
        if len(chosen) == 8:
            break
    documents = []
    queries = []
    for row in chosen:
        customer, invoice, product = (
            row["customer_id"],
            row["invoice_no"],
            row["stock_code"],
        )
        line = f"source-row:{row['source_row']}"
        bill = f"invoice:{invoice}"
        buyer = f"customer:{customer}"
        facts = [
            {
                "subject": f"Product {product}",
                "predicate": "appears on",
                "object": f"Invoice {invoice}",
            },
            {
                "subject": f"Invoice {invoice}",
                "predicate": "belongs to",
                "object": f"Customer {customer}",
            },
            {
                "subject": f"Customer {customer}",
                "predicate": "trades with",
                "object": row["country"],
            },
        ]
        texts = [
            f"Product {product}, {row['description']}, appears on invoice {invoice}. Quantity: {row['quantity']}. Unit price: {row['unit_price']}. Source row {row['source_row']}.",
            f"Invoice {invoice} belongs to customer {customer}. Transaction date: {row['invoice_date']}.",
            f"Customer {customer} trades with {row['country']}. This country is recorded in the original retail transaction.",
        ]
        ids = [line, bill, buyer]
        for identifier, text, fact in zip(ids, texts, facts, strict=True):
            documents.append(
                {
                    "id": identifier,
                    "text": text,
                    "facts": [fact],
                    "customer_id": int(customer),
                    "source_row": int(row["source_row"]),
                    "invoice_date": row["invoice_date"],
                }
            )
        queries.append(
            {
                "id": line,
                "text": f"Which country trades with the customer whose invoice includes product {product} ({row['description']})?",
                "relevant": ids,
            }
        )
    embedder = LocalEmbeddings(cache + "/qwen3")
    reranker = LocalReranker(cache + "/bge-reranker")
    vectors = {}
    profile = ""
    texts = list(
        dict.fromkeys(
            [doc["text"] for doc in documents]
            + [
                f"{fact['subject']} {fact['predicate']} {fact['object']}"
                for doc in documents
                for fact in doc["facts"]
            ]
        )
    )
    for query, values in [
        (False, texts),
        (True, [query["text"] for query in queries]),
    ]:
        for offset in range(0, len(values), 8):
            batch = values[offset : offset + 8]
            result = await embedder.embed(
                EmbedTexts(texts=tuple(batch), query=query)
            )
            if profile and profile != result.profile_id:
                raise ValueError("Embedding profile changed")
            profile = result.profile_id
            for text, vector in zip(batch, result.vectors, strict=True):
                key = hashlib.sha256(
                    f"{int(query)}:{text}".encode()
                ).hexdigest()
                vectors[key] = [round(value, 8) for value in vector]
            print("embedded", query, offset + len(batch), flush=True)
    scores = {}
    for query in queries:
        values = await reranker.scores(
            query["text"], tuple(doc["text"] for doc in documents)
        )
        scores[query["id"]] = {
            doc["id"]: score
            for doc, score in zip(documents, values, strict=True)
        }
        print("reranked", query["id"], flush=True)
    output = {
        "schema_version": 1,
        "source": "https://archive.ics.uci.edu/dataset/352/online+retail",
        "license": "CC-BY-4.0",
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "embedding_profile": profile,
        "reranker_profile": reranker.profile_id,
        "documents": documents,
        "queries": queries,
        "vectors": vectors,
        "reranking": scores,
        "rerankers": {},
        "recognition": {
            "model": "local-eru:pending",
            "queries": {
                query["id"]: {"prompt_sha256": "0" * 64, "facts": []}
                for query in queries
            },
        },
    }
    (root / "retrieval.json").write_text(
        json.dumps(output, separators=(",", ":"), ensure_ascii=False) + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", required=True)
    arguments = parser.parse_args()
    asyncio.run(generate(arguments.cache))
