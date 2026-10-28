"""Generative inference belongs to Scribe; caller schemas remain authoritative."""

import json

import httpx
import pytest
from eru.common.exceptions import ReasoningError
from eru.llm.scribe import ScribeJsonModel
from pydantic import BaseModel


class Answer(BaseModel):
    value: str


def test_scribe_adapter_sends_schema_and_reuses_transport() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://scribe/pipeline/generate"
        body = json.loads(request.content)
        assert body["schema"] == Answer.model_json_schema()
        assert body["prompt"] == "Untrusted evidence"
        assert body["max_tokens"] == 128
        return httpx.Response(200, json={"value": "grounded"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model = ScribeJsonModel("http://scribe", client=client)
        assert model(
            "Untrusted evidence", Answer, max_new_tokens=128
        ) == Answer(value="grounded")


def test_scribe_adapter_rejects_invalid_output_without_local_fallback() -> None:
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"forged": True})
        )
    ) as client:
        with pytest.raises(ReasoningError):
            ScribeJsonModel("http://scribe", client=client)("Evidence", Answer)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
