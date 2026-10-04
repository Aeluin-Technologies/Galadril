"""Regressions for durable chatbot replay assertions."""

import asyncio
from collections.abc import Mapping

import pytest
from chatbot import Generation, _events, decode_generation
from clients import GatewayClient


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
