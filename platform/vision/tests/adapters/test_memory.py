"""Parallel memory writes remain idempotent and cannot invent source bindings."""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from galadril_ontology import GolLease
from galadril_pipeline.config import PipelineStep
from galadril_pipeline.events import PipelineCommand
from galadril_vision.adapters.memory import MemorySink, evidence_units
from galadril_vision.application.errors import CommandProcessingError


def test_units_are_bounded_stable_and_tenant_qualified() -> None:
    raw = {
        "content": "Résumé " * 1000,
        "metadata": {
            "subject": "Meeting",
            "sender": "alice@example.org",
            "password": "hidden",
        },
    }
    first = evidence_units("tenant_a", "record", raw)
    assert first == evidence_units("tenant_a", "record", raw)
    assert first[0]["id"] != evidence_units("tenant_b", "record", raw)[0]["id"]
    assert all(len(unit["text"]) <= 2048 for unit in first)
    with pytest.raises(ValueError):
        evidence_units("tenant_a", "record", {"content": "x" * 300000})


def test_memory_persists_only_after_complete_bound_extraction() -> None:
    async def run(forged: bool) -> None:
        held = [False]

        @asynccontextmanager
        async def protected(
            effects: object, **kwargs: object
        ) -> AsyncIterator[tuple[str, ...]]:
            held[0] = True
            try:
                yield ("signed-receipt",)
            finally:
                held[0] = False

        def respond(request: httpx.Request) -> httpx.Response:
            assert held[0], "LLM I/O escaped its effect boundary"
            data = json.loads(request.content)
            if request.url.path.endswith("extract"):
                return httpx.Response(
                    200,
                    json={
                        "passages": [
                            {
                                "id": "f" * 64 if forged else passage["id"],
                                "facts": [],
                            }
                            for passage in data["passages"]
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "profile_id": "a" * 64,
                    "vectors": [[0.1, 0.2] for _ in data["texts"]],
                },
            )

        config = MagicMock()
        config.connectors.scribe_endpoint = "http://scribe:8090"
        config.ontology_pipeline_id = "mail"
        config.runtime_revision_id = "a" * 32
        client = MagicMock()
        connection = MagicMock()
        cursor = AsyncMock()

        async def write(*args: object, **kwargs: object) -> None:
            assert held[0], "SQL I/O escaped its effect boundary"

        cursor.executemany.side_effect = write
        connection.cursor.return_value.__aenter__.return_value = cursor
        client.tenant_connection.return_value.__aenter__.return_value = (
            connection
        )
        http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        sink = MemorySink(config, http=http)
        step = PipelineStep(
            step="memory",
            type="sink",
            input_from=["source"],
            capability_id="core.output.hipporag2",
            params={"gol_derived_symbol_key": "communication.mail::Message"},
        )
        command = PipelineCommand(
            correlation_id=uuid4(),
            pipeline="mail",
            tenant_id="tenant_a",
            step="memory",
            step_type="sink",
            resource_class="cpu",
            payload={
                "record": {
                    "record_id": "record",
                    "timestamp": "2026-10-26T00:00:00Z",
                    "raw_payload": {
                        "content": "Hello",
                        "metadata": {
                            "sender": "alice@example.org",
                            "subject": "Meeting",
                        },
                    },
                }
            },
        )
        projection = GolLease(
            tenant_id="tenant_a",
            ontology_id="communication.mail",
            ontology_version="1.0.0",
            source_revision="s",
            generation_id="g",
            context_id="server-context",
            symbols=(
                {
                    "key": "communication.mail::Message",
                    "name": "Message",
                    "kind": "entity",
                    "version": "1.0.0",
                },
            ),
        )
        with (
            patch(
                "galadril_vision.adapters.memory.protected_gol_effects",
                side_effect=protected,
                create=True,
            ),
            patch(
                "galadril_vision.adapters.memory.active_gol_lease",
                return_value=projection,
            ),
            patch(
                "galadril_vision.adapters.memory.scoped_instance_id",
                side_effect=lambda value: "scoped-" + value,
            ),
            patch(
                "galadril_vision.adapters.memory.get_pg_stores",
                return_value=(client, None, None),
            ),
        ):
            if forged:
                with pytest.raises(CommandProcessingError):
                    await sink.execute(command, step)
                cursor.executemany.assert_not_called()
            else:
                result = await sink.execute(command, step)
                assert result["indexed_units"] == 1
                saved = cursor.executemany.await_args.args[1]
                assert saved[0][0] == "tenant_a"
                assert saved[0][3] == "scoped-evt_record"
                provenance = json.loads(saved[0][7])
                instance_id = provenance["gol_instance_id"]
                assert instance_id.startswith("scoped-memory:")
                assert (
                    saved[0][2]
                    == hashlib.sha256(instance_id.encode()).hexdigest()
                )
                assert held == [False]
        await http.aclose()

    asyncio.run(run(False))
    asyncio.run(run(True))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
