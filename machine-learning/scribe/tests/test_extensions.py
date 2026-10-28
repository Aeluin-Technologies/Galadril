"""Plugin tools require explicit run selection and stay within native limits."""

from collections.abc import AsyncIterator

import httpx
import pytest
from galadril_pipeline.capabilities import Capability
from galadril_scribe.contracts import RunRequest, Settings
from galadril_scribe.extensions import ToolExtension
from galadril_scribe.runtime import Runtime, create_app
from pydantic import ValidationError
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import FunctionToolset


def _failure_aware_model(names: tuple[str, ...]) -> FunctionModel:
    """A model must adapt its tool choices after failed tools disappear."""

    async def response(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        returned = [
            part
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        if not returned:
            yield {
                index: DeltaToolCall(name=name, json_args="{}")
                for index, name in enumerate(names)
            }
        else:
            visible = {tool.name for tool in info.function_tools}
            for part in returned:
                if part.outcome == "failed":
                    assert part.tool_name not in visible
            yield (
                " ".join(
                    str(part.content)
                    for part in returned
                    if part.outcome == "success"
                )
                or "The operation is unavailable; independent work can continue."
            )

    return FunctionModel(stream_function=response)


def settings() -> Settings:
    return Settings(
        gateway_tools_url="http://gateway/internal/chat/tools",
        models={"local": {"model": "local", "base_url": "http://model/v1"}},
        default_model="local",
    )


def descriptor() -> Capability:
    return Capability.model_validate(
        {
            "id": "law.algorithm.timeline",
            "plugin_id": "law",
            "plugin_version": "1.0.0",
            "kind": "algorithm",
            "targets": ["pipeline", "scribe"],
            "name": "Timeline",
            "description": "Build a timeline.",
            "parameters_schema": {"type": "object"},
        }
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_native_routing_is_discoverable_without_any_plugins() -> None:
    async with httpx.AsyncClient() as client:
        from galadril_plugins import PluginRegistry

        runtime = Runtime(
            settings(),
            client,
            model=TestModel(call_tools=[]),
            plugins=PluginRegistry(()),
        )
        entries = {item.id for item in runtime.capabilities.capabilities}
        assert not any(
            identifier.startswith("routing.") for identifier in entries
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime)),
            base_url="http://scribe",
        ) as api:
            response = await api.get("/pipeline-capabilities")
        assert response.status_code == 200
        ids = {item["id"] for item in response.json()["capabilities"]}
        assert {"core.route.regex", "core.route.slm"} <= ids
        assert not any(identifier.startswith("routing.") for identifier in ids)


@pytest.mark.anyio
async def test_native_agent_features_are_discoverable_without_tool_factories() -> (
    None
):
    async with httpx.AsyncClient() as client:
        runtime = Runtime(settings(), client, model=TestModel(call_tools=[]))
        entries = {item.id: item for item in runtime.capabilities.capabilities}
        for identifier in (
            "core.agent.planning",
            "core.agent.ask-user",
            "core.agent.guardrails",
        ):
            entry = entries[identifier]
            assert entry.plugin_id == "core"
            assert entry.execution == "registered"
            assert tuple(entry.targets) == ("scribe",)


@pytest.mark.anyio
async def test_selected_algorithm_uses_native_tool_loop_and_private_catalog() -> (
    None
):
    calls: list[str] = []

    async def build() -> str:
        calls.append("execute")
        return "traceable chronology"

    def factory() -> FunctionToolset:
        calls.append("factory")
        return FunctionToolset(tools=[build])

    extension = ToolExtension(descriptor(), factory)
    async with httpx.AsyncClient() as client:
        runtime = Runtime(
            settings(),
            client,
            model=TestModel(call_tools=["law_algorithm_timeline_build"]),
            extensions=(extension,),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime)),
            base_url="http://scribe",
        ) as api:
            catalog = await api.get("/capabilities")
            assert catalog.status_code == 200
            assert any(
                item["id"] == extension.capability.id
                for item in catalog.json()["capabilities"]
            )
            assert not calls
        request = RunRequest(
            prompt="Build chronology",
            capability="a" * 64,
            tool_ids=(extension.capability.id,),
        )
        chunks = [chunk async for chunk in runtime.stream(request)]
    assert chunks[-1].kind == "completed"
    assert calls == ["factory", "execute"]
    assert "traceable chronology" in "".join(chunk.content for chunk in chunks)


@pytest.mark.anyio
async def test_unselected_extensions_do_not_create_toolsets() -> None:
    def factory() -> FunctionToolset:
        pytest.fail("Unselected factory must not run")

    async with httpx.AsyncClient() as client:
        runtime = Runtime(
            settings(),
            client,
            model=TestModel(call_tools=[]),
            extensions=(ToolExtension(descriptor(), factory),),
        )
        chunks = [
            chunk
            async for chunk in runtime.stream(
                RunRequest(prompt="Explain", capability="a" * 64)
            )
        ]
    assert chunks[-1].kind == "completed"


@pytest.mark.anyio
async def test_unknown_extension_is_rejected_before_admission() -> None:
    async with httpx.AsyncClient() as upstream:
        runtime = Runtime(settings(), upstream, model=TestModel(call_tools=[]))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime)),
            base_url="http://scribe",
        ) as api:
            for route, extra in [
                ("/runs", {}),
                ("/generations", {"id": "b" * 32}),
            ]:
                response = await api.post(
                    route,
                    json={
                        "prompt": "Run",
                        "capability": "a" * 64,
                        "tool_ids": ["law.algorithm.missing"],
                        **extra,
                    },
                )
                assert response.status_code == 422
                assert runtime.active == 0
                assert not runtime.jobs


def test_request_rejects_duplicate_or_unbounded_selection() -> None:
    for values in [
        ("law.algorithm.timeline",) * 2,
        tuple(f"law.algorithm.t{i}" for i in range(33)),
    ]:
        with pytest.raises(ValidationError):
            RunRequest(prompt="Run", capability="a" * 64, tool_ids=values)


@pytest.mark.anyio
async def test_registry_rejects_colliding_extensions_and_invalid_target() -> (
    None
):
    def factory() -> FunctionToolset:
        return FunctionToolset()

    extension = ToolExtension(descriptor(), factory)
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError):
            Runtime(
                settings(),
                client,
                model=TestModel(),
                extensions=(extension, extension),
            )
        with pytest.raises(ValueError):
            ToolExtension(
                descriptor().model_copy(update={"targets": ("pipeline",)}),
                factory,
            )


@pytest.mark.anyio
async def test_registry_rejects_colliding_normalized_tool_prefixes() -> None:
    def factory() -> FunctionToolset:
        return FunctionToolset()

    first = descriptor().model_copy(update={"id": "law.algorithm.time-line"})
    second = descriptor().model_copy(update={"id": "law.algorithm.time_line"})
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="prefix"):
            Runtime(
                settings(),
                client,
                model=TestModel(),
                extensions=(
                    ToolExtension(first, factory),
                    ToolExtension(second, factory),
                ),
            )


@pytest.mark.anyio
async def test_extension_tool_timeout_cancels_the_native_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    import galadril_scribe.extensions as extensions

    monkeypatch.setattr(
        extensions, "_TOOL_TIMEOUT_SECONDS", 0.01, raising=False
    )
    cancelled: list[bool] = []

    async def build() -> str:
        try:
            await asyncio.sleep(0.2)
            return "late result"
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    def factory() -> FunctionToolset:
        return FunctionToolset(tools=[build])

    async with httpx.AsyncClient() as client:
        runtime = Runtime(
            settings(),
            client,
            model=_failure_aware_model(("law_algorithm_timeline_build",)),
            extensions=(ToolExtension(descriptor(), factory),),
        )
        chunks = [
            chunk
            async for chunk in runtime.stream(
                RunRequest(
                    prompt="Run",
                    capability="a" * 64,
                    tool_ids=(descriptor().id,),
                )
            )
        ]
    assert chunks[-1].kind == "completed"
    assert cancelled == [True]
    assert "late result" not in "".join(chunk.content for chunk in chunks)
    assert any(
        '"source":"tool"' in chunk.content
        for chunk in chunks
        if chunk.kind == "activity"
    )


@pytest.mark.anyio
@pytest.mark.parametrize("approved", [False, True])
@pytest.mark.parametrize("approval_wrapper", [False, True])
async def test_native_pydantic_approval_survives_extension_wrappers(
    approved: bool, approval_wrapper: bool
) -> None:
    from galadril_scribe.extensions import ToolRegistry
    from galadril_scribe.media import StorageMedia
    from galadril_scribe.runtime import Dependencies
    from pydantic_ai import (
        Agent,
        DeferredToolRequests,
        DeferredToolResults,
        Tool,
    )
    from pydantic_ai.toolsets import AbstractToolset

    effects: list[str] = []

    async def build() -> str:
        effects.append("executed")
        return "approved operation"

    def factory() -> AbstractToolset[Dependencies]:
        toolset = FunctionToolset(
            tools=[Tool(build, requires_approval=not approval_wrapper)]
        )
        return toolset.approval_required() if approval_wrapper else toolset

    registry = ToolRegistry((ToolExtension(descriptor(), factory),))
    agent = Agent(
        TestModel(call_tools=["law_algorithm_timeline_build"]),
        deps_type=Dependencies,
        output_type=[str, DeferredToolRequests],
        toolsets=registry.toolsets((descriptor().id,)),
    )
    async with httpx.AsyncClient() as client:
        deps = Dependencies(
            client,
            "http://gateway/internal/chat/tools",
            "a" * 64,
            None,
            StorageMedia(client),
        )
        pending = await agent.run("Execute the selected operation", deps=deps)
        assert isinstance(pending.output, DeferredToolRequests)
        assert len(pending.output.approvals) == 1
        assert not effects
        decision = DeferredToolResults(
            approvals={pending.output.approvals[0].tool_call_id: approved}
        )
        result = await agent.run(
            message_history=pending.all_messages(),
            deferred_tool_results=decision,
            deps=deps,
        )
        assert isinstance(result.output, str)
    assert effects == (["executed"] if approved else [])


@pytest.mark.anyio
async def test_runtime_without_approval_resume_never_executes_protected_tools() -> (
    None
):
    from pydantic_ai import Tool

    effects: list[str] = []

    async def build() -> str:
        effects.append("executed")
        return "must not run"

    def factory() -> FunctionToolset:
        return FunctionToolset(tools=[Tool(build, requires_approval=True)])

    async with httpx.AsyncClient() as client:
        runtime = Runtime(
            settings(),
            client,
            model=TestModel(call_tools=["law_algorithm_timeline_build"]),
            extensions=(ToolExtension(descriptor(), factory),),
        )
        chunks = [
            chunk
            async for chunk in runtime.stream(
                RunRequest(
                    prompt="Execute",
                    capability="a" * 64,
                    tool_ids=(descriptor().id,),
                )
            )
        ]
    assert not effects
    assert chunks[-1].kind == "failed"


@pytest.mark.anyio
async def test_incompatible_user_tools_rejected_before_admission_and_factories() -> (
    None
):
    def factory() -> FunctionToolset:
        pytest.fail("Incompatible tool factory must never run")

    contribution = Capability.model_validate(
        {
            **descriptor().model_dump(),
            "id": "user.algorithm.timeline",
            "origin": "user",
            "plugin_id": None,
            "plugin_version": None,
            "compatibility": {"allowed_models": ["legal"]},
        }
    )
    async with httpx.AsyncClient() as client:
        runtime = Runtime(
            settings(),
            client,
            model=TestModel(call_tools=[]),
            extensions=(ToolExtension(contribution, factory),),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime)),
            base_url="http://scribe",
        ) as api:
            response = await api.get("/capabilities")
            item = next(
                item
                for item in response.json()["capabilities"]
                if item["id"] == contribution.id
            )
            assert item["origin"] == "user"
            assert item["plugin_id"] is None
            for route, extra in [
                ("/runs", {}),
                ("/generations", {"id": "c" * 32}),
            ]:
                result = await api.post(
                    route,
                    json={
                        "prompt": "Run",
                        "capability": "a" * 64,
                        "tool_ids": [contribution.id],
                        **extra,
                    },
                )
                assert result.status_code == 422
                assert runtime.active == 0
                assert not runtime.jobs


@pytest.mark.anyio
async def test_declared_user_skills_are_listed_but_cannot_execute() -> None:
    skill = Capability.model_validate(
        {
            **descriptor().model_dump(),
            "id": "user.skill.research",
            "kind": "skill",
            "origin": "user",
            "plugin_id": None,
            "plugin_version": None,
            "execution": "declared",
        }
    )
    async with httpx.AsyncClient() as client:
        runtime = Runtime(
            settings(),
            client,
            model=TestModel(call_tools=[]),
            declared_capabilities=(skill,),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime)),
            base_url="http://scribe",
        ) as api:
            catalog = (await api.get("/capabilities")).json()
            assert (
                next(
                    item
                    for item in catalog["capabilities"]
                    if item["id"] == skill.id
                )["execution"]
                == "declared"
            )
            response = await api.post(
                "/runs",
                json={
                    "prompt": "Run",
                    "capability": "a" * 64,
                    "tool_ids": [skill.id],
                },
            )
            assert response.status_code == 422
            assert runtime.active == 0
        with pytest.raises(ValueError):
            Runtime(
                settings(),
                client,
                model=TestModel(call_tools=[]),
                declared_capabilities=(descriptor(),),
            )


@pytest.mark.anyio
async def test_compatible_user_tool_executes_with_selected_model() -> None:
    calls: list[str] = []

    async def build() -> str:
        calls.append("executed")
        return "user chronology"

    contribution = Capability.model_validate(
        {
            **descriptor().model_dump(),
            "id": "user.algorithm.timeline",
            "origin": "user",
            "plugin_id": None,
            "plugin_version": None,
            "compatibility": {"allowed_models": ["local"]},
        }
    )
    async with httpx.AsyncClient() as client:
        runtime = Runtime(
            settings(),
            client,
            model=TestModel(call_tools=["user_algorithm_timeline_build"]),
            extensions=(
                ToolExtension(
                    contribution, lambda: FunctionToolset(tools=[build])
                ),
            ),
        )
        chunks = [
            chunk
            async for chunk in runtime.stream(
                RunRequest(
                    prompt="Run",
                    capability="a" * 64,
                    tool_ids=(contribution.id,),
                )
            )
        ]
    assert chunks[-1].kind == "completed"
    assert calls == ["executed"]


@pytest.mark.anyio
async def test_plugin_failure_preserves_other_tools_without_repeating_actions() -> (
    None
):
    effects: list[str] = []

    async def failed() -> str:
        effects.append("failed")
        raise RuntimeError("private upstream detail")

    async def continued() -> str:
        effects.append("continued")
        return "Verified independent result"

    extension = ToolExtension(
        descriptor(), lambda: FunctionToolset(tools=[failed, continued])
    )
    async with httpx.AsyncClient() as client:
        runtime = Runtime(
            settings(),
            client,
            model=_failure_aware_model(
                (
                    "law_algorithm_timeline_failed",
                    "law_algorithm_timeline_continued",
                )
            ),
            extensions=(extension,),
        )
        chunks = [
            chunk
            async for chunk in runtime.stream(
                RunRequest(
                    prompt="Continue independent work",
                    capability="a" * 64,
                    tool_ids=(extension.capability.id,),
                )
            )
        ]
    assert chunks[-1].kind == "completed"
    assert sorted(effects) == ["continued", "failed"]
    assert "private upstream detail" not in "".join(
        chunk.content for chunk in chunks
    )
    assert "Verified independent result" in "".join(
        chunk.content for chunk in chunks
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
