"""Native PydanticAI model and toolset contribution for packaging integration."""

import httpx
from galadril_scribe.contracts import ModelConfig
from galadril_scribe.plugins import RegisteredModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import FunctionToolset


def model(client: httpx.AsyncClient) -> RegisteredModel:
    return RegisteredModel(
        TestModel(custom_output_args={"branch": "match"}, call_tools=[]),
        ModelConfig(model="fixture", base_url="http://fixture/v1"),
    )


def tools() -> FunctionToolset:
    return FunctionToolset()
