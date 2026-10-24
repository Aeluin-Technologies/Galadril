"""Plugin choices use the same validation and model runtime as native features."""

import httpx
import pytest
from galadril_plugins import PluginRegistry
from galadril_scribe.contracts import Settings
from galadril_scribe.runtime import Runtime, create_app
from pydantic_ai.models.test import TestModel


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def settings() -> Settings:
    return Settings(
        gateway_tools_url="http://gateway/internal/chat/tools",
        models={"local": {"model": "local", "base_url": "http://model/v1"}},
        default_model="local",
    )


def registry() -> PluginRegistry:
    return PluginRegistry.from_manifests(
        (
            {
                "id": "fixture",
                "version": "1.0.0",
                "capabilities": [
                    {
                        "id": "fixture.algorithm.count",
                        "plugin_id": "fixture",
                        "plugin_version": "1.0.0",
                        "kind": "algorithm",
                        "targets": ["pipeline"],
                        "step_type": "algorithm",
                        "name": "Count",
                        "description": "Count records.",
                        "parameters_schema": {
                            "type": "object",
                            "properties": {
                                "limit": {"type": "integer", "minimum": 1}
                            },
                            "required": ["limit"],
                            "additionalProperties": False,
                        },
                    }
                ],
                "bindings": {
                    "fixture.algorithm.count": {
                        "pipeline": "galadril_plugins.fixture.handlers.Factory"
                    }
                },
            },
        )
    )


@pytest.mark.anyio
async def test_private_pipeline_validation_checks_version_parameters_and_unknown_ids() -> (
    None
):
    async with httpx.AsyncClient() as client:
        runtime = Runtime(
            settings(),
            client,
            model=TestModel(call_tools=[]),
            plugins=registry(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime)),
            base_url="http://scribe",
        ) as api:
            for identifier, version, limit, status in [
                ("fixture.algorithm.count", "1.0.0", 2, 200),
                ("fixture.algorithm.count", "1.0.0", 0, 422),
                ("fixture.algorithm.count", "2.0.0", 2, 422),
                ("missing.algorithm.count", "1.0.0", 2, 422),
            ]:
                response = await api.post(
                    "/pipeline/validate",
                    json={
                        "definition": {
                            "name": "fixture",
                            "sources": [
                                {
                                    "id": "input",
                                    "schema_path": "text.avsc",
                                    "match_pattern": ".*",
                                }
                            ],
                            "pipeline": [
                                {
                                    "step": "count",
                                    "type": "algorithm",
                                    "input_from": ["input"],
                                    "capability_id": identifier,
                                    "capability_version": version,
                                    "params": {"limit": limit},
                                }
                            ],
                        }
                    },
                )
                assert response.status_code == status
                assert "galadril_plugins.fixture.handlers" not in response.text


@pytest.mark.anyio
async def test_plugin_scribe_model_is_selectable_and_can_route_with_native_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from galadril_plugins import registry as module
    from galadril_scribe.contracts import ModelConfig
    from galadril_scribe.plugins import RegisteredModel

    model = TestModel(custom_output_args={"branch": "legal"}, call_tools=[])
    monkeypatch.setattr(
        module,
        "_load",
        lambda path: (
            lambda client: RegisteredModel(
                model=model,
                config=ModelConfig(
                    model="fixture-model", base_url="http://fixture-model/v1"
                ),
            )
        ),
    )
    plugins = PluginRegistry.from_manifests(
        (
            {
                "id": "fixture",
                "version": "1.0.0",
                "capabilities": [
                    {
                        "id": "fixture.model.classifier",
                        "plugin_id": "fixture",
                        "plugin_version": "1.0.0",
                        "kind": "model",
                        "targets": ["scribe"],
                        "name": "Classifier",
                        "description": "A deployment-owned classifier.",
                        "model_class": "fixture-model",
                        "parameters_schema": {"type": "object"},
                    }
                ],
                "bindings": {
                    "fixture.model.classifier": {
                        "model": "galadril_plugins.fixture.scribe.ModelFactory"
                    }
                },
            },
        )
    )
    async with httpx.AsyncClient() as client:
        runtime = Runtime(
            settings(),
            client,
            model=TestModel(
                custom_output_args={"branch": "other"}, call_tools=[]
            ),
            plugins=plugins,
        )
        assert "fixture.model.classifier" in runtime.models
        assert any(
            item.id == "fixture.model.classifier"
            for item in runtime.capabilities.capabilities
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime)),
            base_url="http://scribe",
        ) as api:
            catalog = await api.get("/models")
            assert any(
                item["alias"] == "fixture.model.classifier"
                for item in catalog.json()["models"]
            )
            response = await api.post(
                "/pipeline/route",
                json={
                    "model_alias": "fixture.model.classifier",
                    "text": "Appeal",
                    "labels": ["legal", "other"],
                    "instructions": "Choose a branch.",
                },
            )
            assert response.status_code == 200
            assert response.json()["model_alias"] == "fixture.model.classifier"
            runtime.models.pop("fixture.model.classifier")
            response = await api.post(
                "/pipeline/route",
                json={
                    "model_alias": "fixture.model.classifier",
                    "fallback_models": ["local"],
                    "text": "Appeal",
                    "labels": ["legal", "other"],
                    "instructions": "Choose a branch.",
                },
            )
            assert response.status_code == 200
            assert response.json()["model_alias"] == "local"
            assert response.json()["fallback"] is True


@pytest.mark.anyio
async def test_missing_default_is_rejected_after_the_complete_plugin_inventory() -> (
    None
):
    dynamic = Settings(
        gateway_tools_url="http://gateway/internal/chat/tools",
        default_model="fixture.model.missing",
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="Default model"):
            Runtime(dynamic, client, plugins=PluginRegistry(()))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
