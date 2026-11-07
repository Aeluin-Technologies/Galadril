"""Plugin choices use the same validation and model runtime as native features."""

from pathlib import Path

import httpx
import pytest
from galadril_pipeline.capabilities import Capability
from galadril_plugins import PluginRegistry
from galadril_plugins.registry import Binding, PluginManifest
from galadril_scribe.contracts import Settings
from galadril_scribe.runtime import Runtime, create_app
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import FunctionToolset


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


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure",
    [
        "missing-tool",
        "missing-skill",
        "model-init",
        "invalid-model",
        "partial-model",
    ],
)
async def test_broken_plugin_never_enters_runtime_catalogs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    from galadril_plugins import registry as module
    from galadril_scribe.contracts import ModelConfig
    from galadril_scribe.plugins import RegisteredModel

    manifests: list[PluginManifest] = []
    for identifier in ("broken", "healthy"):
        tools = Capability.model_validate(
            {
                "id": f"{identifier}.algorithm.count",
                "plugin_id": identifier,
                "plugin_version": "1.0.0",
                "kind": "algorithm",
                "targets": ["pipeline", "scribe"],
                "step_type": "algorithm",
                "name": "Count",
                "description": "Isolation fixture",
                "parameters_schema": {"type": "object"},
            }
        )
        model = Capability.model_validate(
            {
                "id": f"{identifier}.model.example",
                "plugin_id": identifier,
                "plugin_version": "1.0.0",
                "kind": "model",
                "targets": ["scribe"],
                "name": "Example",
                "description": "Isolation fixture",
                "model_class": "fixture-model",
                "parameters_schema": {"type": "object"},
            }
        )
        skill = Capability.model_validate(
            {
                "id": f"{identifier}.skill.example",
                "plugin_id": identifier,
                "plugin_version": "1.0.0",
                "kind": "skill",
                "targets": ["scribe"],
                "name": "Example",
                "description": "Isolation fixture",
                "parameters_schema": {"type": "object"},
            }
        )
        second_model = model.model_copy(
            update={"id": f"{identifier}.model.second"}
        )
        manifests.append(
            PluginManifest(
                id=identifier,
                version="1.0.0",
                capabilities=(tools, model, second_model, skill),
                bindings={
                    tools.id: Binding(
                        pipeline=f"galadril_plugins.{identifier}.pipeline.Factory",
                        scribe=f"galadril_plugins.{identifier}.tools.Factory",
                    ),
                    model.id: Binding(
                        model=f"galadril_plugins.{identifier}.models.Factory"
                    ),
                    second_model.id: Binding(
                        model=f"galadril_plugins.{identifier}.models.Second"
                    ),
                    skill.id: Binding(skill="skill.md"),
                },
            )
        )
        directory = tmp_path / identifier
        directory.mkdir()
        if identifier != "broken" or failure != "missing-skill":
            (directory / "skill.md").write_text(
                "Verified instructions.", encoding="utf-8"
            )

    def load(path: str) -> object:
        broken = path.startswith("galadril_plugins.broken.")
        if ".tools." in path:
            if broken and failure == "missing-tool":
                raise ModuleNotFoundError("private deployment detail")
            return FunctionToolset

        def factory(client: httpx.AsyncClient) -> RegisteredModel | object:
            if broken and (
                failure == "model-init"
                or failure == "partial-model"
                and path.endswith(".Second")
            ):
                raise RuntimeError("private deployment detail")
            if broken and failure == "invalid-model":
                return object()
            return RegisteredModel(
                model=TestModel(
                    custom_output_args={"branch": "match"}, call_tools=[]
                ),
                config=ModelConfig(
                    model="fixture-model", base_url="http://model/v1"
                ),
            )

        return factory

    monkeypatch.setattr(module, "_load", load)
    monkeypatch.setattr(
        module, "files", lambda name: tmp_path / name.rsplit(".", 1)[-1]
    )
    plugins = PluginRegistry(manifests)
    async with httpx.AsyncClient() as client:
        runtime = Runtime(
            settings(), client, model=TestModel(call_tools=[]), plugins=plugins
        )
        assert set(plugins.unavailable) == {"broken"}
        assert "healthy.model.example" in runtime.models
        assert "broken.model.example" not in runtime.models
        for catalog in (runtime.capabilities, runtime.pipeline_capabilities):
            assert not any(
                item.plugin_id == "broken" for item in catalog.capabilities
            )
            assert any(
                item.plugin_id == "healthy" for item in catalog.capabilities
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime)),
            base_url="http://scribe",
        ) as api:
            response = await api.post(
                "/pipeline/route",
                json={
                    "model_alias": "healthy.model.example",
                    "text": "Fixture",
                    "labels": ["match", "other"],
                    "instructions": "Choose a branch.",
                },
            )
            assert response.status_code == 200
            assert response.json()["model_alias"] == "healthy.model.example"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))


_LAW_TEAM = {
    "id": "user.team.test-law",
    "name": "Legal team",
    "agents": [
        {
            "id": "facts",
            "mission": "Review the exhibits.",
            "skill_ids": ["law.skill.faits"],
        }
    ],
}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "kind,media_type",
    [
        ("document", "application/pdf"),
        ("image", "image/png"),
        ("text", "text/plain"),
    ],
)
async def test_private_law_context_refuses_originals_before_model_or_pdf_fetch(
    kind: str, media_type: str
) -> None:
    from galadril_scribe.contracts import RunRequest

    calls: list[str] = []

    async def transport(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise AssertionError(
            "Private originals must never leave the admission boundary"
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport)
    ) as client:
        runtime = Runtime(settings(), client, model=TestModel(call_tools=[]))
        attachment = {
            "kind": kind,
            "media_type": media_type,
            **(
                {"text": "PRIVATE_IDENTITY"}
                if kind == "text"
                else {"url": "https://private.example.org/original.pdf"}
            ),
        }
        request = RunRequest.model_validate(
            {
                "prompt": "PRIVATE_IDENTITY",
                "capability": "a" * 64,
                "execution_context": "law.administratif",
                "team": _LAW_TEAM,
                "tool_ids": ["law.algorithm.dossier"],
                "attachments": [attachment],
                "history": [{"role": "user", "content": "PRIVATE_HISTORY"}],
            }
        )
        with pytest.raises(ValueError, match="privacy preprocessing"):
            runtime.select_model(request)
        assert calls == []
        assert runtime.active == 0


@pytest.mark.anyio
async def test_private_chat_projects_provider_context_before_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import base64
    from collections.abc import AsyncIterator

    from galadril_scribe.contracts import Attachment, RunRequest
    from galadril_scribe.media import StorageMedia
    from galadril_scribe.redaction import RedactionRuntime
    from pydantic_ai.messages import BinaryContent, ModelMessage
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    for file in (
        "gliner_config.json",
        "model.safetensors",
        "spm.model",
        "config.json",
        "tokenizer_config.json",
    ):
        (tmp_path / file).touch()
    calls: list[ModelMessage] = []
    tools: set[str] = set()

    async def response(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str]:
        tools.update(tool.name for tool in info.function_tools)
        calls.extend(messages)
        yield "Evidence reviewed."

    async def redact(
        self: RedactionRuntime, payload: tuple[tuple[str, str], ...], model: str
    ) -> tuple[str, ...]:
        assert model == str(tmp_path)
        result = []
        for kind, value in payload:
            if kind == "pdf":
                assert base64.b64decode(value) == b"%PDF-1.7 Marie Testot"
                result.append("PDF exhibit: M. T.")
            else:
                result.append(value.replace("Marie Testot", "M. T."))
        return tuple(result)

    async def download(
        self: StorageMedia, attachment: Attachment
    ) -> BinaryContent:
        assert (
            attachment.url
            == "https://storage.test/private.pdf?signature=PRIVATE_SIGNATURE"
        )
        return BinaryContent(
            data=b"%PDF-1.7 Marie Testot", media_type="application/pdf"
        )

    monkeypatch.setattr(RedactionRuntime, "run_private", redact)
    monkeypatch.setattr(StorageMedia, "download", download)
    async with httpx.AsyncClient() as client:
        runtime = Runtime(
            settings().model_copy(update={"private_model_dir": str(tmp_path)}),
            client,
            model=FunctionModel(stream_function=response),
        )
        request = RunRequest.model_validate(
            {
                "capability": "a" * 64,
                "execution_context": "law.administratif",
                "team": _LAW_TEAM,
                "tool_ids": ["law.algorithm.dossier"],
                "prompt": "User prompt remains intact.",
                "library_enabled": True,
                "project_instructions": "Use the project instructions.",
                "history": [
                    {
                        "role": "assistant",
                        "content": "Previous chat remains intact.",
                    }
                ],
                "attachments": [
                    {"kind": "text", "text": "Exhibit: Marie Testot."},
                    {
                        "kind": "document",
                        "media_type": "application/pdf",
                        "url": "https://storage.test/private.pdf?signature=PRIVATE_SIGNATURE",
                        "trusted_storage": True,
                    },
                ],
            }
        )
        chunks = [chunk async for chunk in runtime.stream(request)]
        assert calls and any(chunk.kind == "completed" for chunk in chunks)
        assert "Marie Testot" not in repr(calls)
        assert "PRIVATE_SIGNATURE" not in repr(calls)
        assert "PDF exhibit: M. T." in repr(calls)
        assert "read_file" not in tools
        assert "document_search" in tools
        assert any(tool.endswith("validate_dossier") for tool in tools)
        assert "User prompt remains intact." in repr(calls)

        async def unavailable(
            self: RedactionRuntime,
            payload: tuple[tuple[str, str], ...],
            model: str,
        ) -> tuple[str, ...]:
            raise ValueError("Worker refused the original")

        ordinary = request.model_copy(
            update={
                "team": None,
                "tool_ids": (),
                "attachments": [request.attachments[0]],
            }
        )
        calls.clear()
        chunks = [chunk async for chunk in runtime.stream(ordinary)]
        assert any(chunk.kind == "completed" for chunk in chunks)
        assert "Exhibit: Marie Testot." in repr(calls)

        monkeypatch.setattr(RedactionRuntime, "run_private", unavailable)
        calls.clear()
        chunks = [chunk async for chunk in runtime.stream(request)]
        assert not calls
        assert [chunk.kind for chunk in chunks] == ["failed"]
