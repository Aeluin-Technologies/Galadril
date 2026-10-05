"""Black-box chatbot checks over real Gateway, Scribe, PostgreSQL and SpiceDB."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import aiohttp
from assertions import eventually, require_mapping, require_sequence
from clients import (
    GATEWAY_URL,
    OUTSIDER_ID,
    TENANT_ID,
    UPLOADER_ID,
    DerivedPipelineState,
    GatewayClient,
    RelationshipSpec,
    SpiceDBProbe,
    mint_token,
    read_causal_cohort,
    read_causal_state,
)

_EVENTS = """
query Events($conversation: String!, $generation: String!, $after: Int!) {
  generationEvents(conversationId: $conversation, generationId: $generation, after: $after) {
    sequence kind content
  }
}
"""


def decode_generation(events: Sequence[object]) -> dict[str, object] | None:
    """Rejects missing or duplicate persistence before asserting the answer."""
    content: list[str] = []
    for index, item in enumerate(events, start=1):
        event = require_mapping(item, "generation event")
        assert event.get("sequence") == index, (
            "Generation events are not contiguous"
        )
        kind = event.get("kind")
        assert kind in {"content", "completed"}, f"Generation failed: {kind}"
        if kind == "completed":
            assert index == len(events), "Content follows terminal event"
            return require_mapping(
                json.loads("".join(content)), "chatbot answer"
            )
        fragment = event.get("content")
        assert isinstance(fragment, str)
        content.append(fragment)
    return None


@dataclass(frozen=True, slots=True)
class Generation:
    conversation_id: str
    message_id: str
    response_message_id: str


async def _disconnect_after_first_content(
    token: str, conversation_id: str, plan: Mapping[str, object]
) -> Generation:
    """Closes the actual WebSocket while model streaming is still underway."""
    async with (
        aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120)
        ) as client,
        client.ws_connect(
            GATEWAY_URL.replace("http://", "ws://"),
            headers={"authorization": f"Bearer {token}"},
            max_msg_size=262144,
        ) as socket,
    ):
        await socket.send_json({"type": "connection_init"})
        initialized = require_mapping(
            await socket.receive_json(timeout=10), "WebSocket initialization"
        )
        assert initialized.get("type") == "connection_ack"
        await socket.send_json(
            {
                "id": "chat",
                "type": "subscribe",
                "payload": {
                    "query": "subscription Ask($conversation: String!, $prompt: String!) { ask(conversationId: $conversation, prompt: $prompt) { messageId responseMessageId kind content } }",
                    "variables": {
                        "conversation": conversation_id,
                        "prompt": json.dumps(plan),
                    },
                },
            }
        )
        while True:
            frame = require_mapping(
                await socket.receive_json(timeout=60), "chat frame"
            )
            if frame.get("type") == "pong":
                continue
            if frame.get("type") == "ping":
                await socket.send_json({"type": "pong"})
                continue
            assert frame.get("type") == "next", (
                f"Unexpected chat frame: {frame}"
            )
            payload = require_mapping(frame.get("payload"), "chat payload")
            assert not payload.get("errors"), (
                f"Chatbot failed: {payload.get('errors')}"
            )
            event = require_mapping(
                require_mapping(payload.get("data"), "chat data").get("ask"),
                "chat event",
            )
            if event.get("kind") == "CONTENT":
                message_id, response_id = (
                    event.get("messageId"),
                    event.get("responseMessageId"),
                )
                assert isinstance(message_id, str) and isinstance(
                    response_id, str
                )
                return Generation(conversation_id, message_id, response_id)


async def _events(
    gateway: GatewayClient, token: str, generation: Generation, after: int = 0
) -> Sequence[object]:
    events: list[object] = []
    for _page in range(64):
        result = await gateway.execute(
            token,
            _EVENTS,
            {
                "conversation": generation.conversation_id,
                "generation": generation.message_id,
                "after": after,
            },
        )
        page = require_sequence(
            result.get("generationEvents"), "generation events"
        )
        events.extend(page)
        if len(page) < 64:
            return events
        last = require_mapping(page[-1], "last generation event")
        sequence = last.get("sequence")
        assert isinstance(sequence, int) and sequence > after
        after = sequence
        if last.get("kind") in {"completed", "failed"}:
            return events
    raise AssertionError(
        "Generation event history exceeds the bounded replay limit"
    )


async def _ask(
    gateway: GatewayClient, user_id: str, entity_id: str, causal_entity: str
) -> tuple[Generation, dict[str, object]]:
    token = mint_token(user_id)
    created = await gateway.execute(
        token,
        'mutation { createConversation(title: "Chatbot E2E") { conversationId } }',
    )
    conversation = require_mapping(
        created.get("createConversation"), "new conversation"
    )
    conversation_id = conversation.get("conversationId")
    assert isinstance(conversation_id, str)
    generation = await _disconnect_after_first_content(
        token,
        conversation_id,
        {
            "entity_id": entity_id,
            "causal_entity_id": causal_entity,
            "question": "Find the ingested evidence and its causal analysis",
        },
    )

    async def completed() -> dict[str, object] | None:
        return decode_generation(await _events(gateway, token, generation))

    answer = await eventually(
        completed,
        timeout_seconds=120,
        description=f"durable chatbot answer for {user_id} after disconnection",
        abort_on=(AssertionError,),
    )
    events = await _events(gateway, token, generation)
    assert len(events) >= 2
    assert await _events(gateway, token, generation, 1) == events[1:]
    stored = await gateway.execute(
        token,
        "query Conversation($id: String!) { conversation(conversationId: $id) { activeGenerationId messages { messageId role status content createdBy } } }",
        {"id": conversation_id},
    )
    persisted = require_mapping(
        stored.get("conversation"), "completed conversation"
    )
    assert persisted.get("activeGenerationId") is None
    messages = [
        require_mapping(item, "message")
        for item in require_sequence(persisted.get("messages"), "messages")
    ]
    assistant = next(
        item
        for item in messages
        if item.get("messageId") == generation.response_message_id
    )
    assert (
        assistant.get("status") == "completed"
        and assistant.get("role") == "assistant"
    )
    assert assistant.get("createdBy") == user_id
    assert json.loads(str(assistant.get("content"))) == answer
    return generation, answer


def _analysis(answer: Mapping[str, object]) -> dict[str, object]:
    results = require_sequence(
        require_mapping(answer.get("causal"), "causal tool").get("analyses"),
        "analyses",
    )
    assert len(results) == 1
    return require_mapping(
        require_mapping(results[0], "analysis").get("summary"), "causal summary"
    )


def _empty(answer: Mapping[str, object]) -> None:
    assert require_mapping(answer.get("search"), "search").get("evidence") == []
    assert require_mapping(answer.get("graph"), "graph").get("nodes") == []
    assert require_mapping(answer.get("graph"), "graph").get("edges") == []
    assert require_mapping(answer.get("causal"), "causal").get("analyses") == []


async def _policy(
    gateway: GatewayClient,
    spicedb: SpiceDBProbe,
    entity_id: str,
    *,
    active: bool,
) -> None:
    administrator = RelationshipSpec(
        "tenant", TENANT_ID, "administrator", "user", UPLOADER_ID
    )
    await spicedb.touch(administrator)
    try:
        content = (
            "permit(principal, action, resource) when { context.is_structural_allowed == true };\n"
            + f"forbid(principal, action, resource) when {{ context.entity_id == {json.dumps(entity_id)} && context.internal_device == false }};"
        )
        changed = await gateway.execute(
            mint_token(UPLOADER_ID),
            'mutation Policy($content: String!, $active: Boolean!) { setCedarPolicy(policyId: "chatbot_e2e", content: $content, isActive: $active) }',
            {"content": content, "active": active},
        )
        assert changed.get("setCedarPolicy") is True
    finally:
        await spicedb.delete(administrator)


async def exercise_chatbot(
    gateway: GatewayClient, spicedb: SpiceDBProbe, state: DerivedPipelineState
) -> None:
    """Compares retrieval to persisted evidence and tests ReBAC, ABAC and replay."""
    causal_entity = await read_causal_cohort()
    assert causal_entity is not None
    oracle = await read_causal_state(causal_entity)
    assert oracle is not None
    generation, answer = await _ask(
        gateway, UPLOADER_ID, state.entity_id, causal_entity
    )
    evidence = require_sequence(
        require_mapping(answer.get("search"), "search").get("evidence"),
        "evidence",
    )
    assert any(
        require_mapping(item, "evidence").get("entity_id") == state.entity_id
        and require_mapping(
            require_mapping(item, "evidence").get("state"), "state"
        ).get("label")
        == state.state_value.get("label")
        for item in evidence
        if require_mapping(item, "evidence").get("kind") == "entity_state"
    )
    graph = require_mapping(answer.get("graph"), "graph")
    assert any(
        require_mapping(node, "node").get("id") == state.entity_id
        for node in require_sequence(graph.get("nodes"), "nodes")
    )
    summary = _analysis(answer)
    for key in (
        "causal_links",
        "validated_effects",
        "observation_count",
        "counterfactual_ready",
        "root_estimates",
    ):
        assert summary.get(key) == oracle.get(key), (
            f"Chatbot changed persisted causal statistic {key}: "
            f"received {summary.get(key)!r}, persisted {oracle.get(key)!r}"
        )
    assert set(summary) == {
        "causal_links",
        "validated_effects",
        "observation_count",
        "counterfactual_ready",
        "root_estimates",
    }

    _, denied = await _ask(gateway, OUTSIDER_ID, state.entity_id, causal_entity)
    _empty(denied)
    for token in (
        mint_token(OUTSIDER_ID),
        mint_token(UPLOADER_ID, tenant_id="other_tenant"),
    ):
        response = await gateway.request(
            token,
            _EVENTS,
            {
                "conversation": generation.conversation_id,
                "generation": generation.message_id,
                "after": 0,
            },
        )
        assert response.status_code in {401, 403} or require_mapping(
            response.json(), "denied replay"
        ).get("errors")

    reader = RelationshipSpec(
        "entity_state",
        f"{TENANT_ID}/{causal_entity}",
        "reader",
        "user",
        OUTSIDER_ID,
    )
    await spicedb.touch(reader)
    try:
        root_generation, root_answer = await _ask(
            gateway, OUTSIDER_ID, causal_entity, causal_entity
        )
        assert (
            require_mapping(root_answer.get("search"), "raw evidence").get(
                "evidence"
            )
            == []
        )
        assert _analysis(root_answer) == summary
        root_graph = require_mapping(root_answer.get("graph"), "root graph")
        assert all(
            require_mapping(node, "node").get("id") == causal_entity
            for node in require_sequence(root_graph.get("nodes"), "nodes")
        )
        assert root_graph.get("edges") == []
        await _policy(gateway, spicedb, causal_entity, active=True)
        try:
            _, restricted = await _ask(
                gateway, OUTSIDER_ID, causal_entity, causal_entity
            )
            _empty(restricted)
        finally:
            await _policy(gateway, spicedb, causal_entity, active=False)
    finally:
        await spicedb.delete(reader)
    revoked = await gateway.request(
        mint_token(OUTSIDER_ID),
        _EVENTS,
        {
            "conversation": root_generation.conversation_id,
            "generation": root_generation.message_id,
            "after": 0,
        },
    )
    assert require_mapping(revoked.json(), "revoked replay").get("errors"), (
        "Revoked causal answer remains replayable"
    )
