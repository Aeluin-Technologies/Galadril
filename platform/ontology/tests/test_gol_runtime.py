"""Runtime projections require a server-admitted calculation for every effect."""

import json
import unittest
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from galadril_ontology import (
    GatewayGolClient,
    GolBlockContract,
    GolDenied,
    GolEffect,
    GolLease,
    GolRequest,
    GolRuntimeManager,
    GolSymbolKind,
    GolUnavailable,
    active_gol_lease,
    authorized_gol_reads,
    protected_gol_effects,
)
from pydantic import ValidationError


def lease(tenant: str = "tenant-a") -> GolLease:
    return GolLease.model_validate(
        {
            "tenantId": tenant,
            "ontologyId": "communication.mail",
            "ontologyVersion": "1.0.0",
            "sourceRevision": "source-revision",
            "generationId": "generation-one",
            "contextId": "admitted-context",
            "symbols": [
                {
                    "key": "communication.mail::Message",
                    "name": "Message",
                    "kind": "entity",
                    "version": "1.0.0",
                    "references": [],
                    "properties": {},
                    "roles": {},
                }
            ],
            "schemas": {},
        }
    )


class Gateway:
    def __init__(self) -> None:
        self.allowed = True
        self.calls = 0

    async def open(self, request: GolRequest, admission: str) -> GolLease:
        if admission != "server-admission":
            raise GolDenied("GOL runtime unavailable")
        return lease(request.tenant_id)

    @asynccontextmanager
    async def protected(
        self,
        request: GolRequest,
        selected: GolLease,
        admission: str,
        *,
        effects: Sequence[GolEffect],
    ) -> AsyncIterator[tuple[str, ...]]:
        self.calls += 1
        if not self.allowed or admission != "server-admission":
            raise GolDenied("GOL effect unavailable")
        yield tuple("signed-receipt" for _ in effects)


class Runtime(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.worker_key_path = Path(directory.name) / "worker-key"
        self.worker_key_path.write_bytes(b"t" * 32)

    def test_projected_read_keeps_the_full_signed_value_on_the_wire(
        self,
    ) -> None:
        effect = GolEffect(
            operation="read",
            target="communication.mail::Message",
            destination="causal_state",
            purpose="causal_analysis",
            instanceId="message-one",
            proposal={"length": 12, "body": "signed value"},
            receipt="receipt",
            readFields=("length",),
        )
        wire = effect.model_dump(mode="json", by_alias=True, exclude_none=True)
        self.assertEqual(wire["readFields"], ["length"])
        self.assertEqual(
            wire["proposal"], {"length": 12, "body": "signed value"}
        )

    async def test_selected_reads_keep_masks_bound_to_a_live_permit(
        self,
    ) -> None:
        request = WorkerCredential.request()
        client = GatewayGolClient(
            "http://gateway", worker_key_path=self.worker_key_path
        )
        self.addAsyncCleanup(client.close)
        manager = GolRuntimeManager(client)
        effects = tuple(
            GolEffect(
                operation="read",
                target="communication.mail::Message",
                destination="identity_state",
                purpose="identity_resolution",
                instance_id=f"message-{index}",
                proposal={"length": index},
                receipt="signed-value",
            )
            for index in range(2)
        )
        completions: list[bool] = []
        calls = 0

        def transport(outgoing: httpx.Request) -> httpx.Response:
            nonlocal calls
            body = json.loads(outgoing.content)
            if outgoing.url.path.endswith("/runtime"):
                return httpx.Response(
                    200,
                    json=lease().model_dump(mode="json", by_alias=True)
                    | {"contextId": "admitted-context"},
                )
            if "completion" in body:
                self.assertEqual(body["completion"], "read-permit")
                completions.append(body["succeeded"])
                return httpx.Response(200, json={"allowed": True})
            calls += 1
            self.assertIs(body["selectReadable"], True)
            self.assertEqual(len(body["effects"]), 2)
            mask = [True, False] if calls == 1 else [False, False]
            return httpx.Response(
                200,
                json={
                    "allowed": True,
                    "allowedReads": mask,
                    "permit": "read-permit",
                    "receipts": [None, None],
                },
            )

        original_client = httpx.AsyncClient
        with patch.object(
            httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: original_client(
                **kwargs, transport=httpx.MockTransport(transport)
            ),
        ):
            async with manager.bind(request, admission="server-admission"):
                async with authorized_gol_reads(
                    effects, tenant_id=request.tenant_id
                ) as mask:
                    self.assertEqual(mask, (True, False))
                    self.assertEqual(completions, [])
                    with self.assertRaises(GolDenied):
                        async with protected_gol_effects(effects):
                            self.fail("Nested read scope obtained a permit")
                async with authorized_gol_reads(
                    effects, tenant_id=request.tenant_id
                ) as mask:
                    self.assertEqual(mask, (False, False))
        self.assertEqual(completions, [True, True])
        self.assertEqual(calls, 2)

    async def test_transport_read_budget_allows_gateway_to_finish(self) -> None:
        """The worker can receive the result of live checks within the proxy budget."""
        request = WorkerCredential.request()
        original_client = httpx.AsyncClient

        def transport(outgoing: httpx.Request) -> httpx.Response:
            self.assertEqual(
                outgoing.extensions["timeout"],
                {"connect": 10.0, "read": 35.0, "write": 10.0, "pool": 10.0},
            )
            return httpx.Response(
                200,
                json=lease(request.tenant_id).model_dump(by_alias=True)
                | {"contextId": "admitted-context"},
            )

        def connect(
            *,
            timeout: float | httpx.Timeout,
            follow_redirects: bool,
            trust_env: bool,
        ) -> httpx.AsyncClient:
            return original_client(
                timeout=timeout,
                follow_redirects=follow_redirects,
                trust_env=trust_env,
                transport=httpx.MockTransport(transport),
            )

        client = GatewayGolClient(
            "http://api-proxy:8082", worker_key_path=self.worker_key_path
        )
        self.addAsyncCleanup(client.close)
        with patch.object(httpx, "AsyncClient", side_effect=connect):
            self.assertEqual(
                await client.open(request, "private admission"), lease()
            )

    async def test_transport_failure_reports_only_operation_and_category(
        self,
    ) -> None:
        def transport(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/internal/gol/runtime":
                return httpx.Response(403, text="protected response")
            raise httpx.ReadTimeout(
                "protected transport details", request=request
            )

        request = WorkerCredential.request()
        client = GatewayGolClient(
            "http://api-proxy:8082", worker_key_path=self.worker_key_path
        )
        self.addAsyncCleanup(client.close)
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(transport)
        )
        with self.assertLogs("galadril_ontology.client", "WARNING") as logs:
            with self.assertRaises(GolDenied):
                await client.open(request, "private admission")
            with self.assertRaises(GolUnavailable):
                async with client.protected(
                    request,
                    lease(),
                    "private admission",
                    effects=(
                        GolEffect(
                            operation="transfer",
                            target="communication.mail::Message",
                            destination="kafka",
                            purpose="pipeline_dispatch",
                        ),
                    ),
                ):
                    self.fail("An unavailable permit cannot reach I/O")
        self.assertEqual(
            [
                (
                    record.__dict__["event.name"],
                    record.__dict__["phase"],
                    record.__dict__["reason"],
                )
                for record in logs.records
            ],
            [
                ("pipeline.gol.rpc.failed", "open", "http_403"),
                ("pipeline.gol.rpc.failed", "begin", "ReadTimeout"),
            ],
        )
        self.assertNotIn("protected", " ".join(logs.output))
        self.assertNotIn("private admission", " ".join(logs.output))

    async def test_new_instance_ids_are_bound_to_the_admitted_source(
        self,
    ) -> None:
        import hashlib

        import galadril_ontology

        manager = GolRuntimeManager(Gateway())
        request = WorkerCredential.request()
        with self.assertRaises(GolDenied):
            galadril_ontology.scoped_instance_id("evt-one")
        async with manager.bind(request, admission="server-admission"):
            self.assertEqual(
                galadril_ontology.scoped_instance_id("evt-one"),
                "gol:"
                + hashlib.sha256(request.source_object_id.encode()).hexdigest()
                + ":"
                + hashlib.sha256(b"evt-one").hexdigest(),
            )
            with self.assertRaises(GolDenied):
                galadril_ontology.scoped_instance_id("")

    async def test_missing_admission_never_opens_a_calculation(self) -> None:
        manager = GolRuntimeManager(Gateway())
        request = GolRequest(
            source_object_id="raw:tenant-a/source/object",
            tenant_id="tenant-a",
            pipeline_id="mail",
            pipeline_revision_id="revision",
            block_id="sink",
        )
        with self.assertRaises(GolDenied):
            async with manager.bind(request, admission=None):
                self.fail("Missing admission must not reach a handler")

    async def test_revocation_is_checked_for_each_actual_effect(self) -> None:
        gateway = Gateway()
        manager = GolRuntimeManager(gateway)
        request = GolRequest(
            source_object_id="raw:tenant-a/source/object",
            tenant_id="tenant-a",
            pipeline_id="mail",
            pipeline_revision_id="revision",
            block_id="sink",
        )
        writes: list[str] = []
        async with manager.bind(request, admission="server-admission"):
            self.assertEqual(
                active_gol_lease().symbols[0].kind, GolSymbolKind.ENTITY
            )
            async with manager.protected(
                (
                    GolEffect(
                        operation="write",
                        target="communication.mail::Message",
                        destination="eskg",
                        purpose="persistence",
                        proposal={"subject": "approved"},
                        instance_id="message-one",
                    ),
                )
            ):
                writes.append("committed")
            gateway.allowed = False
            with self.assertRaises(GolDenied):
                async with manager.protected(
                    (
                        GolEffect(
                            operation="write",
                            target="communication.mail::Message",
                            destination="eskg",
                            purpose="persistence",
                            proposal={"subject": "approved"},
                            instance_id="message-one",
                        ),
                    )
                ):
                    writes.append("forbidden")
        self.assertEqual(writes, ["committed"])
        self.assertEqual(gateway.calls, 2)
        self.assertIsNone(active_gol_lease())

    async def test_inherited_child_cannot_reuse_a_completed_effect_scope(
        self,
    ) -> None:
        import asyncio

        gateway = Gateway()
        manager = GolRuntimeManager(gateway)
        request = WorkerCredential.request()
        resume = asyncio.Event()
        effects = (
            GolEffect(
                operation="write",
                target="communication.mail::Message",
                destination="eskg",
                purpose="persistence",
                proposal={"subject": "approved"},
                instance_id="message-one",
            ),
        )
        writes: list[str] = []

        async def child() -> None:
            await resume.wait()
            with self.assertRaises(GolDenied):
                async with manager.protected(effects):
                    writes.append("forbidden")

        async with manager.bind(request, admission="server-admission"):
            async with manager.protected(effects):
                task = asyncio.create_task(child())
            resume.set()
            await task
        self.assertEqual(writes, [])
        self.assertEqual(gateway.calls, 1)

    async def test_projection_contains_no_admission_or_runtime_context(
        self,
    ) -> None:
        selected = lease()
        self.assertNotIn(
            "contextId", selected.projection().model_dump(by_alias=True)
        )

    async def test_cross_tenant_and_missing_target_never_enter_scope(
        self,
    ) -> None:
        class WrongTenant(Gateway):
            async def open(
                self, request: GolRequest, admission: str
            ) -> GolLease:
                return lease("tenant-b")

        request = GolRequest(
            source_object_id="raw:tenant-a/source/object",
            tenant_id="tenant-a",
            pipeline_id="p",
            pipeline_revision_id="r",
            block_id="b",
        )
        for manager, contract in (
            (GolRuntimeManager(WrongTenant()), GolBlockContract()),
            (
                GolRuntimeManager(Gateway()),
                GolBlockContract(
                    required_symbol_keys=("communication.mail::Hidden",)
                ),
            ),
        ):
            with self.assertRaises(GolDenied):
                async with manager.bind(
                    request, contract, admission="server-admission"
                ):
                    self.fail(
                        "Unavailable projections must never reach consumers"
                    )
            self.assertIsNone(active_gol_lease())

    async def test_client_bounds_response_and_hides_invalid_metadata(
        self,
    ) -> None:
        request = GolRequest(
            source_object_id="raw:tenant-a/source/object",
            tenant_id="tenant-a",
            pipeline_id="p",
            pipeline_revision_id="r",
            block_id="b",
        )
        for response in (
            httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1)),
            httpx.Response(200, json={"secret_field_name": "hidden"}),
            httpx.Response(403, text="secret_field_name"),
        ):
            client = GatewayGolClient(
                "http://api-proxy:8082", worker_key_path=self.worker_key_path
            )
            client._client = httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _, selected=response: selected
                )
            )
            with self.assertRaises((GolDenied, GolUnavailable)) as failure:
                await client.open(request, "server-admission")
            self.assertNotIn("secret_field_name", str(failure.exception))
            await client.close()

    async def test_legacy_symbol_identifiers_are_rejected(self) -> None:
        invalid = lease().model_dump()
        invalid["symbols"][0]["key"] = "legacy.object"
        with self.assertRaises(ValidationError):
            GolLease.model_validate({**invalid, "contextId": "server-context"})

    async def test_runtime_requests_require_an_exact_source_scope(self) -> None:
        with self.assertRaises(ValidationError):
            GolRequest(
                tenant_id="tenant-a",
                pipeline_id="p",
                pipeline_revision_id="r",
                block_id="b",
            )

    async def test_writes_require_a_business_proposal(self) -> None:
        manager = GolRuntimeManager(Gateway())
        request = GolRequest(
            tenant_id="tenant-a",
            pipeline_id="p",
            pipeline_revision_id="r",
            block_id="b",
            source_object_id="raw:tenant-a/source/object",
        )
        async with manager.bind(request, admission="server-admission"):
            with self.assertRaises(GolDenied):
                async with manager.protected(
                    (
                        GolEffect(
                            operation="write",
                            target="communication.mail::Message",
                            destination="eskg",
                            purpose="persistence",
                            instance_id="message-one",
                        ),
                    )
                ):
                    self.fail("A missing proposal cannot reach I/O")

    async def test_proposals_reject_non_json_or_excessive_values_before_network(
        self,
    ) -> None:
        request = GolRequest(
            tenant_id="tenant-a",
            pipeline_id="p",
            pipeline_revision_id="r",
            block_id="b",
            source_object_id="raw:tenant-a/source/object",
        )
        calls: list[str] = []

        def reject(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            raise AssertionError(
                "Invalid business values must not reach transport"
            )

        for proposal in (
            {"value": float("nan")},
            {"value": 1 << 128},
            {"value": "x" * (128 * 1024)},
        ):
            client = GatewayGolClient(
                "http://api-proxy:8082", worker_key_path=self.worker_key_path
            )
            client._client = httpx.AsyncClient(
                transport=httpx.MockTransport(reject)
            )
            try:
                with self.assertRaises(GolDenied):
                    async with client.protected(
                        request,
                        lease(),
                        "server-admission",
                        effects=(
                            GolEffect(
                                operation="write",
                                target="communication.mail::Message",
                                destination="eskg",
                                purpose="persistence",
                                proposal=proposal,
                                instance_id="message-one",
                            ),
                        ),
                    ):
                        self.fail("Invalid proposals cannot reach I/O")
            finally:
                await client.close()
        self.assertEqual(calls, [])

    async def test_allowed_without_receipt_cannot_reach_a_write(self) -> None:
        request = GolRequest(
            tenant_id="tenant-a",
            pipeline_id="p",
            pipeline_revision_id="r",
            block_id="b",
            source_object_id="raw:tenant-a/source/object",
        )
        client = GatewayGolClient(
            "http://api-proxy:8082", worker_key_path=self.worker_key_path
        )
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "allowed": True,
                        "permit": "held",
                        "receipts": [None],
                    },
                )
            )
        )
        writes: list[str] = []
        try:
            with self.assertRaises(GolDenied):
                async with client.protected(
                    request,
                    lease(),
                    "server-admission",
                    effects=(
                        GolEffect(
                            operation="write",
                            target="communication.mail::Message",
                            destination="eskg",
                            purpose="persistence",
                            proposal={"subject": "approved"},
                            instance_id="message-one",
                        ),
                    ),
                ) as receipts:
                    writes.append(str(receipts))
        finally:
            await client.close()
        self.assertEqual(writes, [])


class WorkerCredential(unittest.IsolatedAsyncioTestCase):
    """Opaque job admission is insufficient without the trusted worker channel."""

    async def test_missing_worker_key_denies_before_network(self) -> None:
        calls: list[str] = []
        client = GatewayGolClient("http://api-proxy:8082")
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: (
                    calls.append(request.url.path) or httpx.Response(200)
                )
            )
        )
        try:
            with self.assertRaises(GolDenied):
                await client.open(self.request(), "server-admission")
        finally:
            await client.close()
        self.assertEqual(calls, [])

    async def test_invalid_worker_key_is_bounded_and_not_disclosed(
        self,
    ) -> None:
        for value in (b"", b"private-key", b"x" * 33, b"x" * 4096):
            with TemporaryDirectory() as directory:
                path = Path(directory) / "worker-key"
                path.write_bytes(value)
                client = GatewayGolClient(
                    "http://api-proxy:8082", worker_key_path=path
                )
                calls: list[str] = []
                client._client = httpx.AsyncClient(
                    transport=httpx.MockTransport(
                        lambda request, calls=calls: (
                            calls.append(request.url.path)
                            or httpx.Response(200)
                        )
                    )
                )
                try:
                    with self.assertRaises(GolDenied) as failure:
                        await client.open(self.request(), "server-admission")
                    self.assertNotIn(directory, str(failure.exception))
                    self.assertNotIn("private-key", str(failure.exception))
                finally:
                    await client.close()
                self.assertEqual(calls, [])

    async def test_both_routes_authenticate_and_refresh_the_operator_key(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "worker-key"
            path.write_bytes(bytes(range(32)))
            calls: list[tuple[str, str]] = []

            def response(request: httpx.Request) -> httpx.Response:
                calls.append(
                    (
                        request.url.path,
                        request.headers.get("x-galadril-gol-worker-key", ""),
                    )
                )
                if request.url.path.endswith("runtime"):
                    return httpx.Response(
                        200,
                        json={
                            **lease().model_dump(mode="json", by_alias=True),
                            "contextId": "admitted-context",
                        },
                    )
                import json

                if "effects" in json.loads(request.content):
                    return httpx.Response(
                        200,
                        json={
                            "allowed": True,
                            "permit": "held",
                            "receipts": [None],
                        },
                    )
                return httpx.Response(200, json={"allowed": True})

            client = GatewayGolClient(
                "http://api-proxy:8082", worker_key_path=path
            )
            client._client = httpx.AsyncClient(
                transport=httpx.MockTransport(response)
            )
            try:
                await client.open(self.request(), "server-admission")
                path.write_bytes(b"r" * 32)
                async with client.protected(
                    self.request(),
                    lease(),
                    "server-admission",
                    effects=(
                        GolEffect(
                            operation="transfer",
                            target="communication.mail::Message",
                            destination="kafka",
                            purpose="pipeline_dispatch",
                        ),
                    ),
                ):
                    pass
            finally:
                await client.close()
            self.assertEqual(
                calls,
                [
                    ("/internal/gol/runtime", bytes(range(32)).hex()),
                    ("/internal/gol/effect", (b"r" * 32).hex()),
                    ("/internal/gol/effect", (b"r" * 32).hex()),
                ],
            )
            self.assertNotIn((b"r" * 32).hex(), repr(client))

    async def test_gateway_unavailability_remains_retryable(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "worker-key"
            path.write_bytes(b"r" * 32)
            client = GatewayGolClient(
                "http://api-proxy:8082", worker_key_path=path
            )
            client._client = httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(503, text="private-backend-error")
                )
            )
            try:
                with self.assertRaises(GolUnavailable) as failure:
                    await client.open(self.request(), "server-admission")
                self.assertNotIn(
                    "private-backend-error", str(failure.exception)
                )
            finally:
                await client.close()

    async def test_batch_permit_encloses_actual_io_and_completes_on_failure(
        self,
    ) -> None:
        import json

        async def run_case(fail: bool) -> None:
            with TemporaryDirectory() as directory:
                path = Path(directory) / "worker-key"
                path.write_bytes(b"t" * 32)
                messages: list[dict[str, object]] = []
                held = False

                def response(request: httpx.Request) -> httpx.Response:
                    nonlocal held
                    body = json.loads(request.content)
                    messages.append(body)
                    if "effects" in body:
                        held = True
                        return httpx.Response(
                            200,
                            json={
                                "allowed": True,
                                "permit": "opaque-permit",
                                "receipts": ["signed-receipt"],
                            },
                        )
                    self.assertEqual(body["completion"], "opaque-permit")
                    self.assertEqual(body["succeeded"], not fail)
                    held = False
                    return httpx.Response(200, json={"allowed": True})

                client = GatewayGolClient(
                    "http://api-proxy:8082", worker_key_path=path
                )
                client._client = httpx.AsyncClient(
                    transport=httpx.MockTransport(response)
                )
                io: list[str] = []

                async def perform() -> None:
                    async with client.protected(
                        self.request(),
                        lease(),
                        "server-admission",
                        effects=(
                            {
                                "operation": "write",
                                "target": "communication.mail::Message",
                                "destination": "eskg",
                                "purpose": "persistence",
                                "instanceId": "message-one",
                                "proposal": {"subject": "approved"},
                            },
                        ),
                    ) as receipts:
                        self.assertTrue(held)
                        self.assertEqual(receipts, ("signed-receipt",))
                        io.append("actual-write")
                        if fail:
                            raise RuntimeError("private-database-failure")

                try:
                    if fail:
                        with self.assertRaises(RuntimeError):
                            await perform()
                    else:
                        await perform()
                finally:
                    await client.close()
                self.assertFalse(held)
                self.assertEqual(io, ["actual-write"])
                self.assertEqual(len(messages), 2)
                self.assertNotIn("private-database-failure", repr(messages))

        await run_case(False)
        await run_case(True)

    async def test_allow_without_permit_never_reaches_io(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "worker-key"
            path.write_bytes(b"t" * 32)
            client = GatewayGolClient(
                "http://api-proxy:8082", worker_key_path=path
            )
            client._client = httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(
                        200, json={"allowed": True, "receipts": ["signed"]}
                    )
                )
            )
            io: list[str] = []
            try:
                with self.assertRaises(GolDenied):
                    async with client.protected(
                        self.request(),
                        lease(),
                        "server-admission",
                        effects=(
                            {
                                "operation": "write",
                                "target": "communication.mail::Message",
                                "destination": "eskg",
                                "purpose": "persistence",
                                "instanceId": "message-one",
                                "proposal": {"subject": "approved"},
                            },
                        ),
                    ):
                        io.append("forbidden")
            finally:
                await client.close()
            self.assertEqual(io, [])

    async def test_cancellation_reports_failure_before_releasing_the_permit(
        self,
    ) -> None:
        import asyncio
        import json

        with TemporaryDirectory() as directory:
            path = Path(directory) / "worker-key"
            path.write_bytes(b"t" * 32)
            started = asyncio.Event()
            blocked = asyncio.Event()
            completed: list[bool] = []

            def response(request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content)
                if "effects" in body:
                    return httpx.Response(
                        200,
                        json={
                            "allowed": True,
                            "permit": "held",
                            "receipts": [None],
                        },
                    )
                completed.append(body["succeeded"])
                return httpx.Response(200, json={"allowed": True})

            client = GatewayGolClient(
                "http://api-proxy:8082", worker_key_path=path
            )
            client._client = httpx.AsyncClient(
                transport=httpx.MockTransport(response)
            )

            async def run() -> None:
                async with client.protected(
                    self.request(),
                    lease(),
                    "server-admission",
                    effects=(
                        {
                            "operation": "transfer",
                            "target": "communication.mail::Message",
                            "destination": "kafka",
                            "purpose": "pipeline_dispatch",
                        },
                    ),
                ):
                    started.set()
                    await blocked.wait()

            task = asyncio.create_task(run())
            try:
                await started.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                await client.close()
            self.assertEqual(completed, [False])

    @staticmethod
    def request() -> GolRequest:
        return GolRequest(
            tenant_id="tenant-a",
            pipeline_id="p",
            pipeline_revision_id="r",
            block_id="b",
            source_object_id="raw:tenant-a/source/object",
        )


if __name__ == "__main__":
    unittest.main()
