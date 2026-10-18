"""Measure one pinned local reranker in a fresh process without recomputing vectors."""

import argparse
import asyncio
import json
import platform
import resource
import sys
import time
from pathlib import Path
from typing import Literal

from galadril_scribe.retrieval_models import LocalReranker


async def generate(cache: str, backend: Literal["bge", "qwen"]) -> None:
    path = Path(__file__).with_name("retrieval.json")
    data = json.loads(path.read_bytes())
    model = LocalReranker(cache + "/" + backend + "-reranker", backend=backend)
    scores = {}
    elapsed = []
    documents = data["documents"]
    for query in data["queries"]:
        started = time.perf_counter()
        values = await model.scores(
            query["text"], tuple(document["text"] for document in documents)
        )
        elapsed.append(time.perf_counter() - started)
        scores[query["id"]] = {
            document["id"]: score
            for document, score in zip(documents, values, strict=True)
        }
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    data.setdefault("rerankers", {})[backend] = {
        "profile": model.profile_id,
        "scores": scores,
        "seconds_per_query": elapsed,
        "peak_rss_bytes": rss if sys.platform == "darwin" else rss * 1024,
        "platform": platform.platform(),
    }
    if backend == "bge":
        data["reranking"] = scores
        data["reranker_profile"] = model.profile_id
    path.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-cache", required=True)
    parser.add_argument("--backend", choices=("bge", "qwen"), required=True)
    arguments = parser.parse_args()
    asyncio.run(generate(arguments.model_cache, arguments.backend))
