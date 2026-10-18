"""Cache local Eru measurements independently of curated retrieval graph fixtures."""

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path

from galadril_scribe.retrieval_contracts import RecognizeFacts
from galadril_scribe.retrieval_eru import LocalEru, recognition_prompt
from retrieval_benchmark import load_benchmark


async def generate(cache: str) -> None:
    root = Path(__file__).parent
    data = load_benchmark()
    path = root / "retrieval.json"
    payload = json.loads(path.read_bytes())
    knowledge = LocalEru(cache + "/eru")
    progress_path = Path(cache) / "eru-progress-v2.json"
    progress = (
        json.loads(progress_path.read_bytes())
        if progress_path.exists()
        else {"documents": {}, "seconds": {}, "recognition": {}}
    )
    for document in data.documents:
        key = hashlib.sha256(document.text.encode()).hexdigest()
        if key not in progress["documents"]:
            started = time.perf_counter()
            result = await knowledge.extract(document.text)
            progress["documents"][key] = result.model_dump(mode="json")
            progress["seconds"][key] = time.perf_counter() - started
            progress_path.write_text(json.dumps(progress, ensure_ascii=False))
        print(
            "extracted",
            document.id,
            len(progress["documents"][key]["facts"]),
            flush=True,
        )
    for query in data.queries:
        request = RecognizeFacts(
            query=query.text,
            facts=tuple(
                {
                    "passage_id": hashlib.sha256(
                        document.id.encode()
                    ).hexdigest(),
                    "ordinal": ordinal,
                    "fact": fact,
                    "similarity": 1.0,
                }
                for document in data.documents
                for ordinal, fact in enumerate(document.facts)
            ),
        )
        prompt = recognition_prompt(request)
        key = hashlib.sha256(prompt.encode()).hexdigest()
        if key not in progress["recognition"]:
            result = await knowledge.recognize(request)
            progress["recognition"][key] = {
                "prompt_sha256": key,
                "facts": [
                    request.facts[index].fact.model_dump()
                    for index in result.indices
                ],
            }
            progress_path.write_text(json.dumps(progress, ensure_ascii=False))
        print(
            "recognized",
            query.id,
            len(progress["recognition"][key]["facts"]),
            flush=True,
        )
    if not knowledge.profile_id:
        await knowledge.extract(data.documents[0].text)
    payload["knowledge"] = {
        "profile": knowledge.profile_id,
        "documents": {
            document.id: progress["documents"][
                hashlib.sha256(document.text.encode()).hexdigest()
            ]["facts"]
            for document in data.documents
        },
        "seconds_per_document": [
            progress["seconds"][
                hashlib.sha256(document.text.encode()).hexdigest()
            ]
            for document in data.documents
        ],
    }
    payload["recognition"] = {
        "model": "local-eru:" + knowledge.profile_id,
        "queries": {
            query.id: progress["recognition"][
                hashlib.sha256(
                    recognition_prompt(
                        RecognizeFacts(
                            query=query.text,
                            facts=tuple(
                                {
                                    "passage_id": hashlib.sha256(
                                        document.id.encode()
                                    ).hexdigest(),
                                    "ordinal": ordinal,
                                    "fact": fact,
                                    "similarity": 1.0,
                                }
                                for document in data.documents
                                for ordinal, fact in enumerate(document.facts)
                            ),
                        )
                    ).encode()
                ).hexdigest()
            ]
            for query in data.queries
        },
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-cache", required=True)
    asyncio.run(generate(parser.parse_args().model_cache))
