"""Real public APIs exercise cached inference against the same ESKG observations."""

import json
import os
from pathlib import Path

import psycopg
from assertions import eventually, require_mapping, require_sequence
from chunking_benchmark import chunking_report, load_chunking
from clients import (
    POSTGRES_DSN,
    TENANT_ID,
    UPLOADER_ID,
    GatewayClient,
    authenticated_token,
)
from hotpot_benchmark import evaluate_hotpot, load_hotpot
from pengolo import _mutation, _upload
from retrieval_benchmark import load_benchmark, recall_at
from upstream_retrieval import compare_upstream


async def _eskg_snapshot() -> tuple[set[tuple[int, int]], tuple[int, int]]:
    async with await psycopg.AsyncConnection.connect(
        POSTGRES_DSN
    ) as connection:
        rows = await connection.execute(
            "SELECT (state_value->>'source_field')::integer,(state_value->'scalar_evidence'->>'source_row')::integer FROM entity_states WHERE tenant_id=%s AND state_value->>'label'='retail-customer'",
            (TENANT_ID,),
        )
        identities = {
            (int(customer), int(row)) for customer, row in await rows.fetchall()
        }
        counts = await connection.execute(
            "SELECT (SELECT count(*) FROM entity_states WHERE tenant_id=%s),(SELECT count(*) FROM entity_embeddings WHERE tenant_id=%s)",
            (TENANT_ID, TENANT_ID),
        )
        count = await counts.fetchone()
        if count is None:
            raise AssertionError("Missing ESKG snapshot")
        return identities, (int(count[0]), int(count[1]))


async def exercise_retrieval(gateway: GatewayClient) -> None:
    data = load_benchmark()
    identities, before = await _eskg_snapshot()
    assert all(
        (document.customer_id, document.source_row) in identities
        for document in data.documents
    ), "Document corpus contradicts the ingested ESKG source"
    sources = {}
    for document in data.documents:
        file = await _upload(
            gateway,
            content=document.text.encode(),
            name=document.id.replace(":", "-") + ".txt",
        )
        identifier = file.get("id")
        assert isinstance(identifier, str)
        sources[document.id] = identifier

    first_source = next(iter(sources.values()))
    submitted = await gateway.execute(
        authenticated_token(UPLOADER_ID),
        "mutation($id:String!){indexDocument(id:$id)}",
        {"id": first_source},
    )
    assert submitted.get("indexDocument") is True

    async def indexed() -> bool | None:
        result = await gateway.execute(
            authenticated_token(UPLOADER_ID),
            "query { pengoloLibrary { items { id data { retrieval { status profileId } } } } }",
        )
        page = require_mapping(result.get("pengoloLibrary"), "library")
        ready = set()
        for value in require_sequence(page.get("items"), "files"):
            file = require_mapping(value, "file")
            metadata = require_mapping(file.get("data"), "metadata").get(
                "retrieval"
            )
            if (
                isinstance(metadata, dict)
                and metadata.get("status") == "ready"
                and metadata.get("profileId") == data.embedding_profile
            ):
                ready.add(file.get("id"))
        return True if set(sources.values()) <= ready else None

    await eventually(
        indexed,
        timeout_seconds=240,
        description="shared retrieval corpus indexing",
    )
    totals = {
        mode: 0.0 for mode in ("lexical", "semantic", "hybrid", "hipporag2")
    }
    selections = []
    for query in data.queries:
        relevant = {sources[identifier] for identifier in query.relevant}
        for mode in totals:
            result = await gateway.execute(
                authenticated_token(UPLOADER_ID),
                "query($input:DocumentSearchInput!){documentSearch(input:$input){hits{sourceId sourceChecksum unitId text similarity relevance graphScore} warnings embeddingProfile rerankerProfile lexicalBackend}}",
                {
                    "input": {
                        "query": query.text,
                        "mode": mode,
                        "limit": 5,
                        "fileIds": list(sources.values()),
                    }
                },
            )
            page = require_mapping(result.get("documentSearch"), "search")
            assert page.get("lexicalBackend") == "bm25"
            if mode != "lexical":
                assert page.get("embeddingProfile") == data.embedding_profile
            ranked = []
            for value in require_sequence(page.get("hits"), "hits"):
                hit = require_mapping(value, "hit")
                identifier = hit.get("sourceId")
                assert (
                    isinstance(identifier, str)
                    and identifier in sources.values()
                )
                ranked.append(identifier)
                if mode == "hipporag2":
                    assert hit.get("graphScore") is not None
            score = recall_at(ranked, relevant, 5)
            totals[mode] += score
            selections.append(
                {
                    "query": query.id,
                    "mode": mode,
                    "recall_at_5": score,
                    "sources": ranked,
                }
            )
    report = {
        "corpus": data.source,
        "queries": len(data.queries),
        "documents": len(data.documents),
        "embedding_profile": data.embedding_profile,
        "reranker_profile": data.reranker_profile,
        "recognition_model": data.recognition.model,
        "recall_at_5": {
            mode: value / len(data.queries) for mode, value in totals.items()
        },
        "results": selections,
        "upstream_graph_reference": compare_upstream(data),
        "chunking_reference": chunking_report(load_chunking()),
        "hotpotqa_distractor_reference": evaluate_hotpot(load_hotpot()),
    }
    print("Retrieval benchmark:", json.dumps(report["recall_at_5"]), flush=True)
    assert totals["hipporag2"] + 1e-9 >= totals["semantic"], (
        "Associative retrieval regressed against the fixed dense baseline"
    )
    outsider = await gateway.execute(
        authenticated_token("e2e-sales-reader"),
        "query($input:DocumentSearchInput!){documentSearch(input:$input){hits{sourceId}}}",
        {
            "input": {
                "query": data.queries[0].text,
                "mode": "hipporag2",
                "fileIds": list(sources.values()),
            }
        },
    )
    assert (
        require_mapping(outsider.get("documentSearch"), "scoped search").get(
            "hits"
        )
        == []
    )
    assert (await _eskg_snapshot()) == (identities, before), (
        "Document indexing polluted ESKG state or entity vectors"
    )
    output = os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR")
    if output:
        (Path(output) / "retrieval-benchmark.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )

    for identifier in sources.values():
        assert (
            await _mutation(gateway, "deletePengoloFile", {"id": identifier})
            is True
        )
