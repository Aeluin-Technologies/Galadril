"""Teams preserve native execution budgets and generation-scoped authority."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from importlib.resources import files

import httpx
import pytest
from galadril_pipeline.capabilities import Capability
from galadril_plugins import PluginRegistry
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
        id="test.team.watch",
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


@pytest.mark.anyio
async def test_legal_workers_are_scoped_and_defense_sees_only_shared_exhibits() -> (
    None
):
    defense = "law.skill.partie-adverse"
    civil = "law.judiciaire.skill.research"
    configured = TeamDefinition(
        id="user.team.case",
        name="Case review",
        agents=(
            AgentDefinition(
                id="defense", mission="PRIVATE_MISSION", skill_ids=(defense,)
            ),
            AgentDefinition(id="analyst", mission="Analyst mission"),
        ),
    )
    visited: set[str] = set()
    sections: list[str] = []
    first_section = asyncio.Event()

    async def model(
        messages: list[ModelMessage], info: AgentInfo
    ) -> ModelResponse:
        instructions = info.instructions or ""
        context = str(messages) + instructions
        assert "FOREIGN_PERSONAL_CONTEXT" not in context
        if "Voici la requête" in context:
            visited.add("defense")
            assert "COMMUNICATED_EXHIBIT" in context
            assert not info.function_tools
            for marker in (
                "PRIVATE_MISSION",
                "PRIVATE_HISTORY",
                "PRIVATE_PROJECT",
                "PRIVATE_PROMPT",
                "PRIVATE_EXHIBIT",
                "ANALYST_REPORT",
            ):
                assert marker not in context
            return ModelResponse(parts=[TextPart("DEFENSE_REPORT")])
        if "Analyst mission" in instructions:
            visited.add("analyst")
            assert "PRIVATE_PROMPT" in context
            assert "DEFENSE_REPORT" not in context
            assert "consult_specialist" not in {
                tool.name for tool in info.function_tools
            }
            if "Check the date" in context:
                await asyncio.wait_for(first_section.wait(), timeout=2)
                visited.add("followup")
                return ModelResponse(parts=[TextPart("DATE_VERIFIED")])
            return ModelResponse(parts=[TextPart("ANALYST_REPORT")])
        assert "ANALYST_REPORT" in context
        visited.add("synthesis")
        if "DEFENSE_REPORT" in context:
            if "After disclosure" in context:
                assert context.count("Specialist unavailable") >= 2
                return ModelResponse(parts=[TextPart("Synthesis")])
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "consult_specialist",
                        {"agent_id": "analyst", "question": "After disclosure"},
                    ),
                ]
            )
        if "DATE_VERIFIED" in context:
            return ModelResponse(
                parts=[ToolCallPart("read_defense_report", {})]
            )
        if "Specialist unavailable" in context:
            return ModelResponse(
                parts=[
                    TextPart("First section."),
                    ToolCallPart(
                        "consult_specialist",
                        {"agent_id": "analyst", "question": "Check the date"},
                    ),
                ]
            )
        assert "consult_specialist" in {
            tool.name for tool in info.function_tools
        }
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "consult_specialist",
                    {"agent_id": "defense", "question": "Share analysis"},
                ),
            ]
        )

    registered = WorkflowSkill(
        capability=Capability.model_validate(
            {
                "id": civil,
                "plugin_id": "law",
                "plugin_version": "0.1.0",
                "kind": "skill",
                "targets": ["scribe"],
                "name": "Civil research",
                "description": "Civil research",
                "parameters_schema": {"type": "object"},
            }
        ),
        instructions="Civil research",
    )
    async with httpx.AsyncClient() as client:
        registry = PluginRegistry.from_manifests(
            [
                json.loads(
                    files("galadril_plugins.law")
                    .joinpath("plugin.json")
                    .read_bytes()
                )
            ]
        )
        runtime = Runtime(
            settings(),
            client,
            model=scripted_model(model),
            skills=(registered,),
            plugins=registry,
        )
        request = RunRequest.model_validate(
            {
                "prompt": "PRIVATE_PROMPT",
                "capability": "a" * 64,
                "litigation_branch": "administratif",
                "team": configured,
                "project_instructions": "PRIVATE_PROJECT",
                "user_instructions": "FOREIGN_PERSONAL_CONTEXT",
                "history": [{"role": "user", "content": "PRIVATE_HISTORY"}],
                "attachments": [
                    {"kind": "text", "text": "COMMUNICATED_EXHIBIT"},
                    {"kind": "text", "text": "PRIVATE_EXHIBIT"},
                ],
                "defense_attachment_indices": [0],
            }
        )
        for branch in (None, "judiciaire", "penal"):
            with pytest.raises(ValueError, match="litigation branch"):
                runtime.select_model(
                    request.model_copy(update={"litigation_branch": branch})
                )
        mixed = configured.model_copy(
            update={
                "agents": (
                    *configured.agents,
                    AgentDefinition(
                        id="civil", mission="Civil research", skill_ids=(civil,)
                    ),
                )
            }
        )
        with pytest.raises(ValueError, match="litigation branch"):
            runtime.select_model(request.model_copy(update={"team": mixed}))
        async for chunk in runtime.stream(request):
            if chunk.kind == "content" and chunk.content.strip():
                sections.append(chunk.content)
                if chunk.content == "First section.":
                    first_section.set()
    assert sections == ["First section.", "Synthesis"]
    assert visited == {"defense", "analyst", "followup", "synthesis"}


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
            team_id="test.team.watch",
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
async def test_public_search_reserves_budget_before_reporting_progress() -> (
    None
):
    from galadril_scribe.dependencies import Dependencies
    from galadril_scribe.media import StorageMedia
    from galadril_scribe.runtime import web_search
    from pydantic_ai import RunContext
    from pydantic_ai.usage import RunUsage

    entered = asyncio.Event()
    resume = asyncio.Event()
    budgets: list[int] = []

    async def progress(operation: str) -> None:
        entered.set()
        await resume.wait()

    async def search(query: str, limit: int) -> str:
        budgets.append(limit)
        return "evidence"

    async with httpx.AsyncClient() as client:
        ctx = RunContext(
            deps=Dependencies(
                client,
                "http://gateway/tools",
                "a" * 64,
                None,
                StorageMedia(client),
                public_search=search,
                web_remaining=1,
                progress=progress,
            ),
            model=TestModel(),
            usage=RunUsage(),
        )
        first = asyncio.create_task(web_search(ctx, "first"))
        await entered.wait()
        second = asyncio.create_task(web_search(ctx, "second"))
        await asyncio.sleep(0)
        resume.set()
        assert await first == "evidence"
        assert "budget exhausted" in await second
    assert budgets == [1]


@pytest.mark.anyio
async def test_private_web_queries_are_blocked_until_exact_approval() -> None:
    from galadril_scribe.dependencies import Dependencies
    from galadril_scribe.media import StorageMedia
    from galadril_scribe.runtime import web_search
    from pydantic_ai import RunContext
    from pydantic_ai.usage import RunUsage

    seen: list[str] = []

    async def search(query: str, limit: int) -> str:
        seen.append(query)
        return "public source"

    async def approve(query: str) -> bool:
        return query == "official public source"

    async with httpx.AsyncClient() as client:
        deps = Dependencies(
            client,
            "",
            "",
            None,
            StorageMedia(client),
            public_search=search,
            web_remaining=8,
            web_approval=approve,
            web_requires_approval=True,
        )
        context = RunContext(deps=deps, model=TestModel(), usage=RunUsage())
        await web_search(context, "private case identifier")
        assert deps.web_remaining == 8
        assert (
            await web_search(context, "official public source")
            == "public source"
        )
        deps.web_closed = True
        await web_search(context, "official public source")
    assert seen == ["official public source"]


def test_secret_policy_checks_keys_and_tool_context() -> None:
    from galadril_scribe.builtin import SecretPolicy
    from pydantic_ai.messages import ToolReturn
    from pydantic_ai_harness.guardrails import ToolResultInfo

    policy = SecretPolicy(("PRIVATE_TOKEN",))
    assert policy.sensitive({"safe": {"PRIVATE_TOKEN": "value"}})
    result = ToolReturn(return_value="public", content="PRIVATE_TOKEN")
    assert (
        policy.result(
            ToolResultInfo(
                name="read_file",
                args={},
                tool_call_id="test",
                result=result,
            )
        ).action
        == "block"
    )


@pytest.mark.anyio
@pytest.mark.parametrize("approved", [False, True])
async def test_attachment_search_waits_for_the_owning_chat(
    approved: bool,
) -> None:
    from galadril_scribe.contracts import GenerationRequest
    from galadril_scribe.interactions import AnswerSubmission

    query = "official sources for a public question"
    sent: list[str] = []
    reviewed = False

    async def transport(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["query"])
        return httpx.Response(200, text="Public result")

    async def model(
        messages: list[ModelMessage], info: AgentInfo
    ) -> ModelResponse:
        if any(
            isinstance(part, ToolReturnPart)
            for message in messages
            for part in message.parts
        ):
            return ModelResponse(parts=[TextPart("Completed search review.")])
        return ModelResponse(
            parts=[ToolCallPart("web_search", {"query": query})]
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport)
    ) as client:
        runtime = Runtime(
            settings(),
            client,
            model=scripted_model(model),
            plugins=PluginRegistry.from_manifests([]),
        )
        request = GenerationRequest.model_validate(
            {
                "id": "b" * 32,
                "capability": "a" * 64,
                "prompt": "Check sources",
                "web_search": True,
                "attachments": [{"kind": "text", "text": "Private exhibit"}],
            }
        )
        async for chunk in runtime.stream(request):
            if chunk.kind != "activity":
                continue
            event = json.loads(chunk.content)
            if event["type"] == "question":
                reviewed = True
                assert not sent
                assert event["questions"][0]["question"].endswith(query)
                runtime.interactions.answer(
                    request.id,
                    request.capability.get_secret_value(),
                    AnswerSubmission.model_validate(
                        {
                            "question_id": event["id"],
                            "answers": [
                                {
                                    "header": "Recherche publique",
                                    "selected": [
                                        "Autoriser" if approved else "Refuser"
                                    ],
                                }
                            ],
                        }
                    ),
                )
    assert reviewed
    assert sent == ([query] if approved else [])


@pytest.mark.anyio
async def test_normal_chat_writes_sections_around_a_calendar_calculation() -> (
    None
):
    async def model(
        messages: list[ModelMessage], info: AgentInfo
    ) -> ModelResponse:
        if "elapsed_calendar_days" in str(messages):
            assert any(
                isinstance(part, ToolReturnPart)
                and part.content
                == {
                    "start": "2024-02-28",
                    "end": "2024-03-01",
                    "elapsed_calendar_days": 2,
                }
                for message in messages
                for part in message.parts
            )
            return ModelResponse(parts=[TextPart("Two calendar days.")])
        return ModelResponse(
            parts=[
                TextPart("First, identify both dates."),
                ToolCallPart(
                    "date_interval",
                    {"start": "2024-02-28", "end": "2024-03-01"},
                ),
            ]
        )

    async with httpx.AsyncClient() as client:
        runtime = Runtime(
            settings(),
            client,
            model=scripted_model(model),
            plugins=PluginRegistry.from_manifests([]),
        )
        output = [
            (chunk.kind, chunk.content)
            async for chunk in runtime.stream(
                RunRequest(prompt="Compare dates", capability="a" * 64)
            )
            if chunk.kind != "activity"
        ]
    assert output == [
        ("content", "First, identify both dates."),
        ("tool", "date_interval"),
        ("content", "\n\n"),
        ("content", "Two calendar days."),
        ("completed", ""),
    ]


@pytest.mark.anyio
async def test_eight_specialists_overlap_before_synthesis() -> None:
    started: set[str] = set()
    ready = asyncio.Event()
    members = tuple(
        AgentDefinition(id=f"worker_{index}", mission=f"Report worker_{index}")
        for index in range(8)
    )
    parallel_team = team().model_copy(update={"agents": members})

    async def model(
        messages: list[ModelMessage], info: AgentInfo
    ) -> ModelResponse:
        for member in members:
            if member.mission in (info.instructions or ""):
                started.add(member.id)
                if len(started) == len(members):
                    ready.set()
                await asyncio.wait_for(ready.wait(), timeout=1)
                return ModelResponse(parts=[TextPart(member.id)])
        assert all(
            f'"content":"{member.id}"' in str(messages) for member in members
        )
        return ModelResponse(parts=[TextPart("Parallel synthesis")])

    async with httpx.AsyncClient() as client:
        runtime = Runtime(
            settings(max_model_requests=9),
            client,
            model=scripted_model(model),
            teams=(parallel_team,),
        )
        chunks = [
            chunk
            async for chunk in runtime.stream(
                RunRequest(
                    prompt="Watch",
                    capability="a" * 64,
                    team_id=parallel_team.id,
                )
            )
        ]
    assert chunks[-1].kind == "completed"
    assert [chunk.content for chunk in chunks if chunk.kind == "content"] == [
        "Parallel synthesis"
    ]


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
                    parts=[
                        ToolCallPart("fixture_algorithm_collect_collect", {})
                    ]
                )
            return ModelResponse(parts=[TextPart(role + " report")])
        text = str(messages)
        assert "news report" in text and "person report" in text
        return ModelResponse(parts=[TextPart("Final synthesis")])

    descriptor = Capability.model_validate(
        {
            "id": "fixture.algorithm.collect",
            "plugin_id": "fixture",
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
    worker_tools = {
        frozenset(tools) for role, tools in seen if role != "orchestrator"
    }
    assert len(worker_tools) == 1
    shared = next(iter(worker_tools))
    assert "fixture_algorithm_collect_collect" in shared
    assert "consult_specialist" not in shared
    assert all(
        frozenset(tools) == shared | {"consult_specialist"}
        for role, tools in seen
        if role == "orchestrator"
    )


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
        id="test.team.restricted",
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
            for identifier in ("test.team.missing", configured.id):
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
            "id": "fixture.algorithm.collect",
            "plugin_id": "fixture",
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
            model=TestModel(call_tools=["fixture_algorithm_collect_collect"]),
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
        id="test.team.email",
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
    assert cancelled == [True, True]
    agents = [
        json.loads(chunk.content)["agent_id"]
        for chunk in chunks
        if chunk.kind == "activity"
        and json.loads(chunk.content).get("phase") == "agent"
    ]
    assert set(agents) == {"news", "person"}


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
        id="test.team.unknown",
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
