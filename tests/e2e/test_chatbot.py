"""Regressions for chatbot streaming and durable replay assertions."""

import asyncio
import json
from collections.abc import Mapping

import httpx
import pengolo
import pytest
from aiohttp import WSMsgType, test_utils, web
from assertions import require_mapping
from chatbot import (
    Generation,
    _disconnect_after_first_content,
    _events,
    decode_generation,
)
from clients import GatewayClient
from pengolo import _mutation as pengolo_mutation


@pytest.mark.parametrize(
    ("status", "warned", "received", "accepted"),
    [
        ("completed", True, False, True),
        ("completed", False, False, False),
        ("completed", True, True, False),
        ("failed", True, False, False),
    ],
)
def test_private_graph_denial_requires_warning_and_no_model_evidence(
    status: str, warned: bool, received: bool, accepted: bool
) -> None:
    events = [
        {
            "kind": "content",
            "content": json.dumps(
                {
                    "_fixture": {"graph_context_received": received},
                }
            ),
        },
    ]
    if warned:
        events.append(
            {
                "kind": "activity",
                "content": json.dumps(
                    {
                        "type": "warning",
                        "source": "graph",
                    }
                ),
            }
        )
    page: dict[str, object] = {"status": status, "events": events}
    if accepted:
        assert pengolo._graph_unavailable(page) is True
    else:
        with pytest.raises(AssertionError):
            pengolo._graph_unavailable(page)


@pytest.mark.parametrize(
    ("status", "message", "accepted"),
    [
        (400, 'In field "reasoningEffort": Unknown field', True),
        (400, 'In field "modelAlias": Expected String', False),
        (503, 'In field "reasoningEffort": Unknown field', False),
    ],
)
def test_pengolo_input_denials_require_precise_graphql_validation(
    monkeypatch: pytest.MonkeyPatch, status: int, message: str, accepted: bool
) -> None:
    class ValidationGateway(GatewayClient):
        __slots__ = ()

        def __init__(self) -> None:
            pass

        async def request(
            self,
            token: str,
            query: str,
            variables: Mapping[str, object] | None = None,
            *,
            trace_id: str | None = None,
        ) -> httpx.Response:
            return httpx.Response(
                status, json={"errors": [{"message": message}]}
            )

    monkeypatch.setattr(
        "pengolo.authenticated_token", lambda actor: "fixture-token"
    )
    operation = pengolo._invalid_input(
        ValidationGateway(),
        "sendPengolo",
        {"reasoningEffort": "high"},
        "reasoningEffort",
    )
    if accepted:
        asyncio.run(operation)
    else:
        with pytest.raises(AssertionError):
            asyncio.run(operation)


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_pengolo_terminal_failure_aborts_polling(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    calls = 0

    async def events(
        gateway: GatewayClient, identifier: str, *, temporary: bool = False
    ) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"status": status, "events": []}

    monkeypatch.setattr(pengolo, "_events", events)

    async def exercise() -> None:
        gateway = GatewayClient()
        try:
            with pytest.raises(
                RuntimeError, match="Pengolo generation is terminal"
            ):
                await pengolo.eventually(
                    lambda: pengolo._completed(
                        gateway, "generation", temporary=True
                    ),
                    timeout_seconds=1,
                    description="terminal generation",
                    abort_on=(pengolo.GenerationFailure,),
                )
            assert calls == 1
        finally:
            await gateway.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("terminal_frame", "error"),
    [
        (
            {
                "type": "next",
                "id": "chat",
                "payload": {
                    "data": {
                        "ask": {
                            "kind": "CONTENT",
                            "messageId": "generation",
                            "responseMessageId": "response",
                            "content": "first fragment",
                        }
                    }
                },
            },
            None,
        ),
        ({"type": "error", "id": "chat"}, "Unexpected chat frame"),
        ({"type": "complete", "id": "chat"}, "Unexpected chat frame"),
        (
            {"type": "next", "id": "chat", "payload": {"errors": ["failed"]}},
            "Chatbot failed",
        ),
    ],
)
def test_stream_handles_keepalive_without_hiding_subscription_failures(
    monkeypatch: pytest.MonkeyPatch,
    terminal_frame: dict[str, object],
    error: str | None,
) -> None:
    async def exercise() -> None:
        received: list[dict[str, object]] = []
        disconnected: asyncio.Future[WSMsgType] = (
            asyncio.get_running_loop().create_future()
        )

        async def stream(request: web.Request) -> web.WebSocketResponse:
            assert request.headers.get("Origin") == (
                f"{request.scheme}://{request.host}"
            )
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            received.append(
                require_mapping(await socket.receive_json(), "init")
            )
            await socket.send_json({"type": "connection_ack"})
            await socket.send_json({"type": "pong"})
            received.append(
                require_mapping(await socket.receive_json(), "subscribe")
            )
            await socket.send_json({"type": "ping"})
            received.append(
                require_mapping(await socket.receive_json(), "pong")
            )
            await socket.send_json({"type": "pong"})
            await socket.send_json(terminal_frame)
            closing = await socket.receive(timeout=5)
            disconnected.set_result(closing.type)
            await socket.close()
            return socket

        application = web.Application()
        application.router.add_get("/graphql", stream)
        async with test_utils.TestServer(application) as server:
            monkeypatch.setattr(
                "chatbot.GATEWAY_URL", str(server.make_url("/graphql"))
            )
            if error is None:
                generation = await _disconnect_after_first_content(
                    "fixture-token",
                    "conversation",
                    {"question": "find evidence"},
                )
                assert generation == Generation(
                    "conversation", "generation", "response"
                )
            else:
                with pytest.raises(AssertionError, match=error):
                    await _disconnect_after_first_content(
                        "fixture-token",
                        "conversation",
                        {"question": "find evidence"},
                    )
            assert (
                await asyncio.wait_for(disconnected, timeout=5)
                == WSMsgType.CLOSE
            )
        assert [frame.get("type") for frame in received] == [
            "connection_init",
            "subscribe",
            "pong",
        ]

    asyncio.run(asyncio.wait_for(exercise(), timeout=10))


def test_replay_waits_for_completion_and_decodes_ordered_fragments() -> None:
    first = {"sequence": 1, "kind": "content", "content": '{"answer":'}
    second = {"sequence": 2, "kind": "content", "content": '"évidence"}'}
    assert decode_generation([first, second]) is None
    assert decode_generation(
        [first, second, {"sequence": 3, "kind": "completed", "content": ""}]
    ) == {"answer": "évidence"}


@pytest.mark.parametrize(
    "events",
    [
        [{"sequence": 1, "kind": "failed", "content": ""}],
        [{"sequence": 2, "kind": "content", "content": "{}"}],
        [
            {"sequence": 1, "kind": "content", "content": "{}"},
            {"sequence": 1, "kind": "completed", "content": ""},
        ],
        [
            {"sequence": 1, "kind": "completed", "content": "{}"},
            {"sequence": 2, "kind": "content", "content": "{}"},
        ],
    ],
)
def test_replay_rejects_failed_missing_duplicate_or_late_events(
    events: list[dict[str, object]],
) -> None:
    with pytest.raises(AssertionError):
        decode_generation(events)


def test_replay_follows_all_bounded_pages() -> None:
    class PagedGateway(GatewayClient):
        def __init__(self) -> None:
            self.cursors: list[int] = []

        async def execute(
            self,
            token: str,
            query: str,
            variables: Mapping[str, object] | None = None,
            *,
            trace_id: str | None = None,
        ) -> dict[str, object]:
            assert variables is not None
            after = variables.get("after")
            assert isinstance(after, int)
            self.cursors.append(after)
            return {
                "generationEvents": [
                    {"sequence": index, "kind": "content", "content": ""}
                    for index in range(after + 1, min(after + 65, 132))
                ]
            }

    gateway = PagedGateway()
    events = asyncio.run(
        _events(
            gateway,
            "token",
            Generation("conversation", "generation", "response"),
        )
    )
    assert len(events) == 131
    assert gateway.cursors == [0, 64, 128]


@pytest.mark.parametrize(
    ("response", "expected"),
    [(True, True), (False, False)],
)
@pytest.mark.parametrize(
    ("field", "input_type", "value"),
    [
        ("deletePengoloFile", "PengoloFileInput", {"id": "file-id"}),
        (
            "deletePengoloProject",
            "PengoloProjectDeletionInput",
            {"id": "project-id", "revision": 3},
        ),
    ],
)
def test_pengolo_mutation_preserves_boolean_acknowledgements(
    monkeypatch: pytest.MonkeyPatch,
    response: object,
    expected: bool,
    field: str,
    input_type: str,
    value: dict[str, object],
) -> None:
    class MutationGateway(GatewayClient):
        def __init__(self) -> None:
            pass

        async def execute(
            self,
            token: str,
            query: str,
            variables: Mapping[str, object] | None = None,
            *,
            trace_id: str | None = None,
        ) -> dict[str, object]:
            assert token == "fixture-token"
            assert f"{field}(input: $input)" in query
            assert f"{input_type}!" in query
            assert variables == {"input": value}
            return {field: response}

    def authenticated_token(actor: str) -> str:
        assert actor == "fixture-uploader"
        return "fixture-token"

    monkeypatch.setattr("pengolo.authenticated_token", authenticated_token)
    result = asyncio.run(
        pengolo_mutation(
            MutationGateway(),
            field,
            value,
            actor="fixture-uploader",
        )
    )
    assert result is expected


@pytest.mark.parametrize(
    "response", [True, False, 1, None, [], "true", "false"]
)
def test_pengolo_object_mutations_reject_scalar_responses(
    monkeypatch: pytest.MonkeyPatch, response: object
) -> None:
    class MutationGateway(GatewayClient):
        def __init__(self) -> None:
            pass

        async def execute(
            self,
            token: str,
            query: str,
            variables: Mapping[str, object] | None = None,
            *,
            trace_id: str | None = None,
        ) -> dict[str, object]:
            return {"finishPengoloUpload": response}

    def authenticated_token(actor: str) -> str:
        return "fixture-token"

    monkeypatch.setattr("pengolo.authenticated_token", authenticated_token)
    with pytest.raises(AssertionError, match="must be an object"):
        asyncio.run(
            pengolo._object_mutation(
                MutationGateway(), "finishPengoloUpload", {"id": "file-id"}
            )
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
