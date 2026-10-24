"""Native branch handlers preserve payloads and delegate model selection to Scribe."""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from galadril_pipeline.config import PipelineStep
from galadril_pipeline.events import PipelineCommand
from galadril_vision.adapters.routing import RegexRouter, SlmRouter


def step(strategy: str = "regex") -> PipelineStep:
    return PipelineStep.model_validate(
        {
            "step": "choose",
            "type": "route",
            "routing": strategy,
            "model": "local" if strategy == "slm" else None,
            "input_from": ["input"],
            "branches": {"legal": ["law"], "general": ["archive"]},
            "params": {
                "field": "record.text",
                **(
                    {
                        "pattern": r"dossier (?P<case>\d+)",
                        "match_branch": "legal",
                        "default_branch": "general",
                    }
                    if strategy == "regex"
                    else {
                        "instructions": "Classify",
                        "fallback_models": ["api"],
                    }
                ),
            },
        }
    )


def command(text: str = "dossier 42") -> PipelineCommand:
    return PipelineCommand(
        correlation_id=uuid4(),
        tenant_id="a",
        pipeline="p",
        step="choose",
        step_type="route",
        resource_class="cpu",
        payload={"record": {"text": text}},
    )


def test_regex_extracts_and_selects_only_a_declared_branch() -> None:
    definition = step()
    handler = RegexRouter(definition)
    event = command()
    result = asyncio.run(handler.execute(event, definition))
    assert result["branch"] == "legal"
    assert result["extractions"] == {"case": "42"}
    assert result["record"] is event.payload["record"]
    assert (
        asyncio.run(handler.execute(command("unmatched"), definition))["branch"]
        == "general"
    )


def test_regex_input_and_runtime_are_bounded() -> None:
    definition = step()
    handler = RegexRouter(definition)
    with pytest.raises(ValueError, match="bounded"):
        asyncio.run(handler.execute(command("a" * 262145), definition))
    definition.params.model_extra["pattern"] = "(a+)+$"
    definition.params.model_extra["timeout_seconds"] = 0.001
    with pytest.raises(TimeoutError):
        asyncio.run(
            RegexRouter(definition).execute(
                command("a" * 60000 + "!"), definition
            )
        )


def test_slm_uses_scribe_aliases_and_records_the_actual_fallback() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url == "http://scribe/pipeline/route"
        assert body["model_alias"] == "local"
        assert body["fallback_models"] == ["api"]
        assert body["labels"] == ["legal", "general"]
        assert "api_key" not in body
        return httpx.Response(
            200,
            json={"branch": "legal", "model_alias": "api", "fallback": True},
        )

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        ) as client:
            definition = step("slm")
            event = command()
            result = await SlmRouter(
                definition, "http://scribe", client=client
            ).execute(event, definition)
            assert result["routing_model"] == "api"
            assert result["routing_fallback"] is True
            assert result["record"] is event.payload["record"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "body",
    [
        {"branch": "outside", "model_alias": "api", "fallback": True},
        {"branch": "legal"},
    ],
)
def test_scribe_cannot_return_an_undeclared_or_incomplete_decision(
    body: dict[str, object],
) -> None:
    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=body)
            )
        ) as client:
            definition = step("slm")
            with pytest.raises(ValueError):
                await SlmRouter(
                    definition, "http://scribe", client=client
                ).execute(command(), definition)

    asyncio.run(run())


def test_compiled_transfer_preserves_the_same_payload() -> None:
    from galadril_vision.adapters.routing import ForwardingStepAdapter

    definition = PipelineStep.model_validate(
        {"step": "send", "type": "forward", "input_from": ["input"]}
    )
    event = command()
    assert (
        asyncio.run(ForwardingStepAdapter().execute(event, definition))
        is event.payload
    )


def test_regex_can_select_a_metadata_title_without_using_document_text() -> (
    None
):
    definition = step()
    definition.params.model_extra["field"] = "record.metadata.title"
    event = command("unrelated content")
    event.payload["record"]["metadata"] = {"title": "dossier 42"}
    result = asyncio.run(RegexRouter(definition).execute(event, definition))
    assert result["branch"] == "legal"


def test_metadata_conditions_match_expected_values_without_type_coercion() -> (
    None
):
    from galadril_vision.adapters.routing import MetadataRouter

    definition = step().model_copy(update={"routing": "metadata"})
    definition.params.model_extra.clear()
    definition.params.model_extra.update(
        {
            "conditions": {
                "record.metadata.date": "2026-10-23",
                "record.metadata.title": "Appeal",
            },
            "match_branch": "legal",
            "default_branch": "general",
        }
    )
    event = command()
    event.payload["record"]["metadata"] = {
        "date": "2026-10-23",
        "title": "Appeal",
        "private": "excluded",
    }
    handler = MetadataRouter(definition)
    assert asyncio.run(handler.execute(event, definition))["branch"] == "legal"
    event.payload["record"]["metadata"]["date"] = 20261023
    assert (
        asyncio.run(handler.execute(event, definition))["branch"] == "general"
    )


def test_slm_receives_document_content_and_only_selected_metadata() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        content = json.loads(json.loads(request.content)["text"])
        assert content == {
            "content": "dossier 42",
            "metadata": {"record.metadata.title": "Appeal"},
        }
        return httpx.Response(
            200,
            json={"branch": "legal", "model_alias": "local", "fallback": False},
        )

    async def run() -> None:
        definition = step("slm")
        definition.params.model_extra["metadata_fields"] = [
            "record.metadata.title"
        ]
        event = command()
        event.payload["record"]["metadata"] = {
            "title": "Appeal",
            "private": "excluded",
        }
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        ) as client:
            await SlmRouter(definition, "http://scribe", client=client).execute(
                event, definition
            )

    asyncio.run(run())


def test_absent_optional_metadata_does_not_prevent_slm_content_routing() -> (
    None
):
    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(json.loads(request.content)["text"])
        assert body["content"] == "dossier 42"
        assert body["metadata"] == {"record.metadata.title": None}
        return httpx.Response(
            200,
            json={"branch": "legal", "model_alias": "local", "fallback": False},
        )

    async def run() -> None:
        definition = step("slm")
        definition.params.model_extra["metadata_fields"] = [
            "record.metadata.title"
        ]
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        ) as client:
            await SlmRouter(definition, "http://scribe", client=client).execute(
                command(), definition
            )

    asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
