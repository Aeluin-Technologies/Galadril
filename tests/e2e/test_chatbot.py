"""Regressions for chatbot streaming and durable replay assertions."""

import asyncio
from collections.abc import Mapping

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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
