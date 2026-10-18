"""Real public APIs exercise cached inference against the same ESKG observations."""

import asyncio
import hashlib
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
from official_memory import compare_memory, load_memory
from pengolo import _mutation, _upload
from retrieval_benchmark import Benchmark, load_benchmark, recall_at
from upstream_retrieval import compare_upstream


async def _eskg_snapshot() -> tuple[
    dict[tuple[int, int], str], tuple[int, int]
]:
    async with await psycopg.AsyncConnection.connect(
        POSTGRES_DSN
    ) as connection:
        rows = await connection.execute(
            "SELECT entity_id::text,(state_value->>'source_field')::integer,(state_value->'scalar_evidence'->>'source_row')::integer FROM entity_states WHERE tenant_id=%s AND state_value->>'label'='retail-customer'",
            (TENANT_ID,),
        )
        identities: dict[tuple[int, int], str] = {}
        for entity, customer, row in await rows.fetchall():
            key = (int(customer), int(row))
            resolved = str(entity)
            previous = identities.get(key)
            assert previous is None or previous == resolved, (
                "Ambiguous ESKG source identity"
            )
            identities[key] = resolved
        counts = await connection.execute(
            "SELECT (SELECT count(*) FROM entity_states WHERE tenant_id=%s),(SELECT count(*) FROM entity_embeddings WHERE tenant_id=%s)",
            (TENANT_ID, TENANT_ID),
        )
        count = await counts.fetchone()
        if count is None:
            raise AssertionError("Missing ESKG snapshot")
        return identities, (int(count[0]), int(count[1]))


async def _bind_document_evidence(
    data: Benchmark, sources: dict[str, str]
) -> dict[tuple[int, int], tuple[str, str]]:
    """Attach source-derived fixture documents to actual observations without inventing identity links."""
    async with await psycopg.AsyncConnection.connect(
        POSTGRES_DSN
    ) as connection:
        cursor = await connection.execute(
            "SELECT entity_id::text,event_id,(state_value->>'source_field')::integer,(state_value->'scalar_evidence'->>'source_row')::integer FROM entity_states WHERE tenant_id=%s AND state_value->>'label'='retail-customer'",
            (TENANT_ID,),
        )
        bindings: dict[tuple[int, int], tuple[str, str]] = {}
        for entity, event, customer, row in await cursor.fetchall():
            key = (int(customer), int(row))
            value = (str(entity), str(event))
            assert key not in bindings or bindings[key] == value, (
                "Ambiguous source observation"
            )
            bindings[key] = value
        events: dict[str, list[dict[str, str]]] = {}
        for document in data.documents:
            entity, event = bindings[
                (document.customer_id, document.source_row)
            ]
            assert entity
            events.setdefault(event, []).append(
                {
                    "source_id": sources[document.id],
                    "checksum": hashlib.sha256(
                        document.text.encode()
                    ).hexdigest(),
                }
            )
        for event, references in events.items():
            updated = await connection.execute(
                "UPDATE eskg_events SET properties=jsonb_set(properties,'{document_evidence}',%s::jsonb) WHERE tenant_id=%s AND event_id=%s RETURNING event_id",
                (json.dumps(references), TENANT_ID, event),
            )
            assert len(await updated.fetchall()) == 1, "Missing source event"
    return bindings


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

    bindings = await _bind_document_evidence(data, sources)

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
        mode: 0.0
        for mode in (
            "lexical",
            "semantic",
            "hybrid",
            "hipporag2",
            "hipporag2_eskg",
        )
    }
    selections = []
    for query in data.queries:
        relevant = {sources[identifier] for identifier in query.relevant}
        for mode in totals:
            result = await gateway.execute(
                authenticated_token(UPLOADER_ID),
                "query($input:DocumentSearchInput!){documentSearch(input:$input){hits{sourceId sourceChecksum unitId text similarity relevance graphScore eskgRefs{entityId eventId stateType}} warnings embeddingProfile rerankerProfile lexicalBackend engine}}",
                {
                    "input": {
                        "query": query.text,
                        "mode": "hipporag2"
                        if mode == "hipporag2_eskg"
                        else mode,
                        "includeEskg": mode == "hipporag2_eskg",
                        "limit": 5,
                        "fileIds": list(sources.values()),
                    }
                },
            )
            page = require_mapping(result.get("documentSearch"), "search")
            assert page.get("lexicalBackend") == "bm25"
            if mode.startswith("hipporag2"):
                assert page.get("engine") == "hipporag2-official", page
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
                if mode.startswith("hipporag2"):
                    assert hit.get("graphScore") is not None
                refs = require_sequence(hit.get("eskgRefs"), "ESKG references")
                if mode != "hipporag2_eskg":
                    assert not refs
                else:
                    assert refs, (
                        "Explicit source-linked ESKG evidence is missing"
                    )
                    document = next(
                        document
                        for document in data.documents
                        if sources[document.id] == identifier
                    )
                    expected = bindings[
                        (document.customer_id, document.source_row)
                    ]
                    pairs = {
                        (
                            require_mapping(ref, "ESKG reference").get(
                                "entityId"
                            ),
                            require_mapping(ref, "ESKG reference").get(
                                "eventId"
                            ),
                        )
                        for ref in refs
                    }
                    assert expected in pairs
                    assert all(event == expected[1] for _, event in pairs)
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
        "upstream_graph_reference": compare_upstream(
            data, eskg_bindings=identities
        ),
        "official_memory": await asyncio.to_thread(
            compare_memory, data, load_memory()[1], bindings=bindings
        ),
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
