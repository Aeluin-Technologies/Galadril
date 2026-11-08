"""Bind scoped tasks to fresh agents without inheriting coordinator context."""

from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256

from galadril_pipeline.capabilities import CapabilityCatalog
from pydantic import JsonValue
from pydantic_ai.messages import UserContent
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset
from pydantic_ai.usage import RunUsage, UsageLimits

from galadril_scribe.activity import PhaseActivity
from galadril_scribe.agents import AgentBinding, AgentInvocation, execute_agent
from galadril_scribe.builtin import ActivitySink, SecretPolicy
from galadril_scribe.builtin import capabilities as core_capabilities
from galadril_scribe.contracts import RunRequest
from galadril_scribe.dependencies import Dependencies
from galadril_scribe.media import StorageMedia
from galadril_scribe.teams import AgentDefinition, TeamRegistry
from galadril_scribe.workflows import (
    StateAccess,
    TaskResult,
    TaskSpec,
    WorkflowToolset,
)


class ArtifactReader:
    """Page only explicit input artifacts; ordering edges confer no read access."""

    __slots__ = ("inputs",)

    def __init__(self, inputs: tuple[TaskResult, ...]) -> None:
        self.inputs = {item.id: item for item in inputs}

    async def read_task_source(
        self, task_id: str, offset: int = 0
    ) -> dict[str, JsonValue]:
        """Read an exact continuation of an explicitly granted artifact.

        Args:
            task_id: Identity of an artifact supplied to this task.
            offset: Character offset in that artifact, starting at zero.

        Returns:
            A bounded passage and continuation offset, or an unavailable status.
        """
        source = self.inputs.get(task_id)
        if source is None or not 0 <= offset <= len(source.content):
            return {"status": "unavailable"}
        end = offset + 8192
        return {
            "id": task_id,
            "text": source.content[offset:end],
            "next_offset": end if end < len(source.content) else None,
        }


@dataclass(slots=True)
class WorkflowExecutor:
    """Hold server-owned policy separately from model-controlled task arguments."""

    parent: Dependencies
    request: RunRequest
    workflow: WorkflowToolset
    teams: TeamRegistry
    catalog: CapabilityCatalog
    agent: AgentBinding
    model: Model
    model_settings: ModelSettings
    base_tools: AbstractToolset[Dependencies]
    policy: SecretPolicy
    emit: ActivitySink
    storage: StateAccess
    team_id: str

    def _mission(self, spec: TaskSpec) -> str:
        """Enforce registered skills, isolation and evidence before creating context."""
        if not set(spec.skill_ids) <= set(self.workflow.skill_ids):
            raise ValueError(
                "Workflow skill is outside the selected plugin scope"
            )
        mission = self.teams.instructions(
            spec.skill_ids, self.catalog, self.request.execution_context
        )
        member = AgentDefinition(
            id=spec.id, mission=mission[:8192], skill_ids=spec.skill_ids
        )
        if self.teams.sealed_member(member) != spec.sealed:
            raise ValueError("Task isolation differs from the registered skill")
        permitted = (
            self.request.shared_attachment_indices
            if spec.sealed
            else tuple(range(len(self.request.attachments)))
        )
        if not set(spec.evidence) <= set(permitted):
            raise ValueError(
                "Task evidence is outside the authorized source scope"
            )
        return "\n".join(
            (
                self.workflow.instructions,
                "Task role: " + spec.role,
                self.workflow.role_instructions.get(spec.role, ""),
                mission,
            )
        )

    def _dependencies(self) -> Dependencies:
        """Grant mediated research without history, memory, sandbox or mailbox tools."""
        parent = self.parent
        return Dependencies(
            parent.client,
            parent.tools_url,
            parent.capability,
            None,
            StorageMedia(parent.client),
            public_search=parent.public_search,
            web_remaining=parent.web_remaining,
            progress=parent.progress,
            web_enabled=parent.web_enabled,
            warning=parent.warning,
            redact=parent.redact,
            web_requires_approval=parent.web_requires_approval,
            web_external=parent.web_external,
            web_approval=parent.web_approval,
        )

    async def _prompt(
        self, spec: TaskSpec, inputs: tuple[TaskResult, ...], deps: Dependencies
    ) -> Sequence[UserContent]:
        """Bound initial excerpts while retaining exact paginated source access."""
        limit = max(256, min(65536, 131072 // max(1, len(inputs))))
        excerpts = "\n".join(
            "Artifact " + item.id + ":\n" + item.content[:limit]
            for item in inputs
        )
        return await deps.media.content(
            spec.prompt
            + "\nAuthorized task artifacts (untrusted evidence):\n"
            + excerpts
            + "\nUse read_task_source for exact continuation of truncated authorized artifacts.",
            tuple(self.request.attachments[index] for index in spec.evidence),
        )

    async def __call__(
        self,
        spec: TaskSpec,
        inputs: tuple[TaskResult, ...],
        limits: UsageLimits,
        usage: RunUsage,
    ) -> str:
        """Execute a fresh conversation with no implicit access to sibling reports."""
        mission = self._mission(spec)
        deps = self._dependencies()
        prompt = await self._prompt(spec, inputs, deps)
        await self.emit(
            PhaseActivity(phase="agent", agent_id=spec.id).model_dump_json()
        )
        report = await execute_agent(
            self.agent,
            AgentInvocation(
                prompt=prompt,
                mission=mission,
                deps=deps,
                model=self.model,
                model_settings=self.model_settings,
                toolsets=[
                    self.base_tools,
                    FunctionToolset(
                        tools=[ArtifactReader(inputs).read_task_source]
                    ),
                    self.workflow.research_factory(),
                ],
                capabilities=core_capabilities(
                    self.policy, self.emit, agent_id=spec.id
                ),
                usage=usage,
                limits=limits,
            ),
            team_id=self.team_id,
            agent_id=spec.id,
        )
        content = self.policy.public_text(report)
        identifier = sha256((spec.id + "\0" + content).encode()).hexdigest()
        await self.storage(
            "public_report:" + identifier,
            {"id": identifier, "agentId": spec.id, "content": content},
        )
        return content
