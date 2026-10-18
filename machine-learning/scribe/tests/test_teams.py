"""Teams preserve native execution budgets and generation-scoped authority."""

import json
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
import pytest
from galadril_pipeline.capabilities import Capability
from galadril_scribe.contracts import RunRequest, Settings
from galadril_scribe.extensions import ToolExtension
from galadril_scribe.runtime import Runtime, create_app
from galadril_scribe.teams import AgentDefinition, TeamDefinition, WorkflowSkill
from pydantic import ValidationError
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import FunctionToolset


def scripted_model(
    response: Callable[
        [list[ModelMessage], AgentInfo], Awaitable[ModelResponse]
    ],
) -> FunctionModel:
    async def stream(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        result = await response(messages, info)
        for part in result.parts:
            if isinstance(part, TextPart):
                yield part.content
            elif isinstance(part, ToolCallPart):
                yield {
                    0: DeltaToolCall(
                        name=part.tool_name,
                        json_args=part.args
                        if isinstance(part.args, str)
                        else json.dumps(part.args),
                    )
                }

    return FunctionModel(response, stream_function=stream)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def settings(**updates: object) -> Settings:
    return Settings.model_validate(
        {
            "gateway_tools_url": "http://gateway/tools",
            "models": {
                "local": {"model": "local", "base_url": "http://model/v1"}
            },
            "default_model": "local",
            **updates,
        }
    )


def team() -> TeamDefinition:
    return TeamDefinition(
        id="law.team.watch",
        name="Watch",
        agents=(
            AgentDefinition(id="news", mission="Monitor public news"),
            AgentDefinition(id="person", mission="Follow public activity"),
        ),
    )


def skill() -> WorkflowSkill:
    return WorkflowSkill(
        capability=Capability.model_validate(
            {
                "id": "user.skill.research",
                "origin": "user",
                "kind": "skill",
                "targets": ["scribe"],
                "name": "Research",
                "description": "Verify chronology before reporting",
                "parameters_schema": {"type": "object"},
            }
        ),
        instructions="PRIVATE_WORKFLOW: Verify dates and cite every source.",
    )


def test_delegated_team_cannot_select_trusted_implementations() -> None:
    payload = team().model_dump(mode="json")
    payload["id"] = "user.team.watch"
    payload["agents"][0]["implementation_id"] = "law.agent.privileged"
    with pytest.raises(ValidationError):
        RunRequest(prompt="Watch", capability="a" * 64, team=payload)
    payload["agents"][0]["implementation_id"] = None
    request = RunRequest(prompt="Watch", capability="a" * 64, team=payload)
    assert request.team is not None
    with pytest.raises(ValidationError):
        RunRequest(
            prompt="Watch",
            capability="a" * 64,
            team=payload,
            team_id="law.team.watch",
        )


@pytest.mark.anyio
async def test_specialist_skill_can_require_a_shared_native_tool() -> None:
    definition = skill().model_dump(mode="json")
    definition["capability"]["compatibility"]["requires"] = ["core.tool.search"]
    selected = WorkflowSkill.model_validate(definition)
    delegated = TeamDefinition(
        id="user.team.research",
        name="Research",
        agents=(
            AgentDefinition(
                id="legal",
                mission="Research",
                skill_ids=(selected.capability.id,),
            ),
        ),
    )
    async with httpx.AsyncClient() as client:
        runtime = Runtime(
            settings(), client, model=TestModel(), skills=(selected,)
        )
        runtime.select_model(
            RunRequest(prompt="Research", capability="a" * 64, team=delegated)
        )


@pytest.mark.anyio
async def test_delegated_team_uses_registered_plugin_skills_without_registration() -> (
    None
):
    selected = skill()
    configured = TeamDefinition(
        id="user.team.watch",
        name="Legal watch",
        agents=(
            AgentDefinition(
                id="legal",
                mission="Research case law",
                skill_ids=(selected.capability.id,),
            ),
        ),
    )
    async with httpx.AsyncClient() as client:
        runtime = Runtime(
            settings(), client, model=TestModel(), skills=(selected,)
        )
        request = RunRequest(
            prompt="Watch", capability="a" * 64, team=configured
        )
        runtime.select_model(request)
        assert runtime.teams.catalog.teams == ()
        bad = configured.model_copy(
            update={
                "agents": (
                    AgentDefinition(
                        id="legal",
                        mission="Research case law",
                        skill_ids=("law.skill.missing",),
                    ),
                )
            }
        )
        with pytest.raises(ValueError):
            runtime.select_model(request.model_copy(update={"team": bad}))


@pytest.mark.anyio
async def test_workers_share_authorized_tools_and_only_orchestrator_publishes() -> (
    None
):
    seen: list[tuple[str, tuple[str, ...]]] = []

    async def collect() -> str:
        return "verified evidence"

    async def model(
        messages: list[ModelMessage], info: AgentInfo
    ) -> ModelResponse:
        role = (
            "news"
            if "Monitor public news" in (info.instructions or "")
            else "person"
            if "Follow public activity" in (info.instructions or "")
            else "orchestrator"
        )
        seen.append((role, tuple(tool.name for tool in info.function_tools)))
        if role != "orchestrator":
            assert not any(
                isinstance(part, TextPart)
                and "old private reply" in part.content
                for message in messages
                for part in message.parts
            )
            if not any(
                isinstance(part, ToolReturnPart)
                for message in messages
                for part in message.parts
            ):
                return ModelResponse(
                    parts=[ToolCallPart("law_algorithm_collect_collect", {})]
                )
            return ModelResponse(parts=[TextPart(role + " report")])
        text = str(messages)
        assert "news report" in text and "person report" in text
        return ModelResponse(parts=[TextPart("Final synthesis")])

    descriptor = Capability.model_validate(
        {
            "id": "law.algorithm.collect",
            "plugin_id": "law",
            "plugin_version": "1",
            "kind": "algorithm",
            "targets": ["scribe"],
            "name": "Collect",
            "description": "Collect evidence",
            "parameters_schema": {"type": "object"},
        }
    )
    async with httpx.AsyncClient() as client:
        runtime = Runtime(
            settings(),
            client,
            model=scripted_model(model),
            teams=(team(),),
            extensions=(
                ToolExtension(
                    descriptor, lambda: FunctionToolset(tools=[collect])
                ),
            ),
        )
        chunks = [
            chunk
            async for chunk in runtime.stream(
                RunRequest(
                    prompt="Watch developments",
                    capability="a" * 64,
                    team_id=team().id,
                    tool_ids=(descriptor.id,),
                    history=[
                        {"role": "assistant", "content": "old private reply"}
                    ],
                )
            )
        ]
    assert chunks[-1].kind == "completed"
    assert (
        "".join(chunk.content for chunk in chunks if chunk.kind == "content")
        == "Final synthesis"
    )
    assert {role for role, tools in seen} == {"news", "person", "orchestrator"}
    assert len({tools for role, tools in seen}) == 1


@pytest.mark.anyio
async def test_each_agent_only_receives_its_own_deferred_workflow() -> None:
    selected = skill()
    configured = team().model_copy(
        update={
            "agents": (
                AgentDefinition(
                    id="news",
                    mission="Monitor public news",
                    skill_ids=(selected.capability.id,),
                ),
                team().agents[1],
            )
        }
    )
    loaded: list[str] = []

    async def model(
        messages: list[ModelMessage], info: AgentInfo
    ) -> ModelResponse:
        instructions = info.instructions or ""
        names = {tool.name for tool in info.function_tools}
        if "Monitor public news" in instructions:
            if (
                "PRIVATE_WORKFLOW" not in instructions
                and "PRIVATE_WORKFLOW" not in str(messages)
            ):
                assert "load_capability" in names
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "load_capability",
                            {"id": selected.capability.id},
                        )
                    ]
                )
            loaded.append("news")
        else:
            assert "PRIVATE_WORKFLOW" not in instructions
            assert "load_capability" not in names
        return ModelResponse(parts=[TextPart("Report")])

    async with httpx.AsyncClient() as client:
        runtime = Runtime(
            settings(),
            client,
            model=scripted_model(model),
            teams=(configured,),
            skills=(selected,),
        )
        chunks = [
            chunk
            async for chunk in runtime.stream(
                RunRequest(
                    prompt="Watch", capability="a" * 64, team_id=configured.id
                )
            )
        ]
    assert chunks[-1].kind == "completed"
    assert loaded == ["news"]


@pytest.mark.anyio
async def test_unknown_team_or_incompatible_skill_rejected_before_admission() -> (
    None
):
    restricted = WorkflowSkill(
        capability=Capability.model_validate(
            skill().capability.model_dump()
            | {"compatibility": {"allowed_models": ["other"]}}
        ),
        instructions="Check sources",
    )
    configured = TeamDefinition(
        id="law.team.restricted",
        name="Restricted",
        agents=(
            AgentDefinition(
                id="news",
                mission="Monitor news",
                skill_ids=(restricted.capability.id,),
            ),
        ),
    )
    async with httpx.AsyncClient() as client:
        runtime = Runtime(
            settings(),
            client,
            model=TestModel(call_tools=[]),
            teams=(configured,),
            skills=(restricted,),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime)),
            base_url="http://scribe",
        ) as api:
            assert (await api.get("/teams")).json()["teams"][0][
                "id"
            ] == configured.id
            for identifier in ("law.team.missing", configured.id):
                for route, extra in [
                    ("/runs", {}),
                    ("/generations", {"id": "b" * 32}),
                ]:
                    response = await api.post(
                        route,
                        json={
                            "prompt": "Watch",
                            "capability": "a" * 64,
                            "team_id": identifier,
                            **extra,
                        },
                    )
                    assert response.status_code == 422
                    assert runtime.active == 0
                    assert not runtime.jobs


@pytest.mark.anyio
async def test_team_shares_one_tool_budget_and_cannot_synthesize_after_failure() -> (
    None
):
    effects: list[str] = []

    async def collect() -> str:
        effects.append("called")
        return "evidence"

    descriptor = Capability.model_validate(
        {
            "id": "law.algorithm.collect",
            "plugin_id": "law",
            "plugin_version": "1",
            "kind": "algorithm",
            "targets": ["scribe"],
            "name": "Collect",
            "description": "Collect",
            "parameters_schema": {"type": "object"},
        }
    )
    async with httpx.AsyncClient() as client:
        runtime = Runtime(
            settings(max_tool_calls=1),
            client,
            model=TestModel(call_tools=["law_algorithm_collect_collect"]),
            teams=(team(),),
            extensions=(
                ToolExtension(
                    descriptor, lambda: FunctionToolset(tools=[collect])
                ),
            ),
        )
        chunks = [
            chunk
            async for chunk in runtime.stream(
                RunRequest(
                    prompt="Watch",
                    capability="a" * 64,
                    team_id=team().id,
                    tool_ids=(descriptor.id,),
                )
            )
        ]
    assert chunks[-1].kind == "failed"
    assert len(effects) == 1
    assert not any(chunk.kind == "content" for chunk in chunks)


def test_team_definitions_bound_work_and_reject_authority_fields() -> None:
    for updates in (
        {"agents": []},
        {"agents": [team().agents[0]] * 2},
        {
            "agents": [
                AgentDefinition(id=f"a{i}", mission="Watch") for i in range(9)
            ]
        },
        {"endpoint": "http://private"},
    ):
        with pytest.raises(ValidationError):
            TeamDefinition.model_validate(team().model_dump() | updates)
    with pytest.raises(ValidationError):
        AgentDefinition(id="news", mission="Watch", tool_ids=("law.mcp.admin",))
    with pytest.raises(ValidationError):
        WorkflowSkill(capability=skill().capability, instructions="x" * 32769)


@pytest.mark.anyio
async def test_plugin_native_agent_preserves_structured_output_and_validators() -> (
    None
):
    from galadril_scribe.agents import NativeAgentBinding
    from galadril_scribe.runtime import Dependencies
    from pydantic import BaseModel
    from pydantic_ai import Agent

    class EmailReport(BaseModel):
        sender: str
        subject: str

    validated: list[EmailReport] = []
    native = Agent(deps_type=Dependencies, output_type=EmailReport)

    @native.output_validator
    async def validate_report(report: EmailReport) -> EmailReport:
        validated.append(report)
        return report

    async def response(
        messages: list[ModelMessage], info: AgentInfo
    ) -> ModelResponse:
        if info.output_tools:
            assert {"search", "graph", "causal"}.issubset(
                {tool.name for tool in info.function_tools}
            )
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        info.output_tools[0].name,
                        {"sender": "alice", "subject": "Legal update"},
                    )
                ]
            )
        text = str(messages)
        assert "alice" in text and "Legal update" in text
        return ModelResponse(parts=[TextPart("Structured synthesis")])

    configured = TeamDefinition(
        id="law.team.email",
        name="Email",
        agents=(
            AgentDefinition(
                id="email",
                mission="Extract email metadata",
                implementation_id="utils.agent.email",
            ),
        ),
    )
    async with httpx.AsyncClient() as client:
        runtime = Runtime(
            settings(),
            client,
            model=scripted_model(response),
            teams=(configured,),
            agent_bindings={"utils.agent.email": NativeAgentBinding(native)},
        )
        chunks = [
            chunk
            async for chunk in runtime.stream(
                RunRequest(
                    prompt="Analyze email",
                    capability="a" * 64,
                    team_id=configured.id,
                )
            )
        ]
    assert chunks[-1].kind == "completed"
    assert len(validated) == 1
    assert validated[0].sender == "alice"
    assert (
        "".join(chunk.content for chunk in chunks if chunk.kind == "content")
        == "Structured synthesis"
    )


@pytest.mark.anyio
async def test_team_spans_report_usage_without_recording_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from opentelemetry.trace import StatusCode

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(
        "galadril_scribe.runtime.trace.get_tracer", provider.get_tracer
    )
    try:
        async with httpx.AsyncClient() as client:
            runtime = Runtime(
                settings(),
                client,
                model=TestModel(
                    call_tools=[], custom_output_text="PRIVATE_RESULT"
                ),
                teams=(team(),),
            )
            chunks = [
                chunk
                async for chunk in runtime.stream(
                    RunRequest(
                        prompt="PRIVATE_REQUEST",
                        capability="a" * 64,
                        team_id=team().id,
                    )
                )
            ]
        assert chunks[-1].kind == "completed"
        spans = [
            span
            for span in exporter.get_finished_spans()
            if span.name == "scribe.team.agent"
        ]
        assert len(spans) == 2
        assert all(span.status.status_code == StatusCode.OK for span in spans)
        assert all(
            span.attributes is not None
            and span.attributes["scribe.usage.requests"] == 1
            for span in spans
        )
        assert {
            span.attributes["scribe.agent.id"]
            for span in spans
            if span.attributes
        } == {"news", "person"}
        recorded = str(
            [
                (span.attributes, span.events)
                for span in exporter.get_finished_spans()
            ]
        )
        assert (
            "PRIVATE_REQUEST" not in recorded
            and "PRIVATE_RESULT" not in recorded
        )
        assert "a" * 64 not in recorded
    finally:
        provider.shutdown()


@pytest.mark.anyio
async def test_global_deadline_cancels_worker_and_stops_the_team() -> None:
    import asyncio

    cancelled: list[bool] = []

    async def slow_model(
        messages: list[ModelMessage], info: AgentInfo
    ) -> ModelResponse:
        try:
            await asyncio.sleep(10)
            return ModelResponse(parts=[TextPart("Late")])
        finally:
            cancelled.append(True)

    async with httpx.AsyncClient() as client:
        runtime = Runtime(
            settings(run_timeout_seconds=1),
            client,
            model=scripted_model(slow_model),
            teams=(team(),),
        )
        chunks = [
            chunk
            async for chunk in runtime.stream(
                RunRequest(
                    prompt="Watch", capability="a" * 64, team_id=team().id
                )
            )
        ]
    assert chunks[-1].kind == "failed"
    assert cancelled == [True]
    agents = [
        json.loads(chunk.content)["agent_id"]
        for chunk in chunks
        if chunk.kind == "activity"
        and json.loads(chunk.content).get("phase") == "agent"
    ]
    assert agents == ["news"]


@pytest.mark.anyio
async def test_oversized_agent_reports_never_reach_the_orchestrator() -> None:
    async with httpx.AsyncClient() as client:
        runtime = Runtime(
            settings(),
            client,
            model=TestModel(call_tools=[], custom_output_text="x" * 16385),
            teams=(team(),),
        )
        chunks = [
            chunk
            async for chunk in runtime.stream(
                RunRequest(
                    prompt="Watch", capability="a" * 64, team_id=team().id
                )
            )
        ]
    assert chunks[-1].kind == "failed"
    assert not any(chunk.kind == "content" for chunk in chunks)


@pytest.mark.anyio
async def test_unknown_native_agent_binding_rejected_during_composition() -> (
    None
):
    configured = TeamDefinition(
        id="law.team.unknown",
        name="Unknown",
        agents=(
            AgentDefinition(
                id="email",
                mission="Extract metadata",
                implementation_id="utils.agent.missing",
            ),
        ),
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError):
            Runtime(
                settings(),
                client,
                model=TestModel(call_tools=[]),
                teams=(configured,),
            )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
