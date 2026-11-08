"""Exercise scoped delegation independently of domain prompts and live models."""

import asyncio
import unittest
from decimal import Decimal

from galadril_scribe.workflow_execution import ArtifactReader
from galadril_scribe.workflows import TaskGraph, TaskResult, TaskSpec
from pydantic import JsonValue
from pydantic_ai.usage import RunUsage, UsageLimits


class WorkflowTest(unittest.IsolatedAsyncioTestCase):
    async def test_zero_remaining_cost_and_tokens_stop_before_model_access(
        self,
    ) -> None:
        async def storage(key: str, value: JsonValue | None) -> JsonValue:
            return None

        async def execute(
            spec: TaskSpec,
            inputs: tuple[TaskResult, ...],
            limits: UsageLimits,
            usage: RunUsage,
        ) -> str:
            self.fail("Exhausted consumption must not call a model")

        for limits, usage in (
            (
                UsageLimits(request_limit=20, cost_limit=Decimal("0.5")),
                RunUsage(cost=Decimal("0.5")),
            ),
            (
                UsageLimits(request_limit=20, total_tokens_limit=100),
                RunUsage(input_tokens=100),
            ),
        ):
            graph = TaskGraph(execute, storage, usage, limits)
            with self.assertRaisesRegex(ValueError, "consumption"):
                await graph.run(
                    TaskSpec(id="bounded", skill_ids=("fixture.skill.norms",))
                )

    async def test_exact_artifact_pages_never_read_sibling_reports(
        self,
    ) -> None:
        reader = ArtifactReader(
            (
                TaskResult(
                    id="draft",
                    content="a" * 8192 + "EXACT_END",
                    output_kind="draft",
                ),
            )
        )
        first = await reader.read_task_source("draft")
        self.assertEqual(len(first["text"]), 8192)
        self.assertEqual(first["next_offset"], 8192)
        last = await reader.read_task_source("draft", 8192)
        self.assertEqual(last["text"], "EXACT_END")
        self.assertIsNone(last["next_offset"])
        for task, offset in (("private", 0), ("draft", -1), ("draft", 99999)):
            self.assertEqual(
                await reader.read_task_source(task, offset),
                {"status": "unavailable"},
            )

    async def test_dependency_order_does_not_implicitly_disclose_its_report(
        self,
    ) -> None:
        async def storage(key: str, value: JsonValue | None) -> JsonValue:
            return None

        async def execute(
            spec: TaskSpec,
            inputs: tuple[TaskResult, ...],
            limits: UsageLimits,
            usage: RunUsage,
        ) -> str:
            if spec.id == "child":
                self.assertEqual(inputs, ())
                return "Independent"
            return "INTERNAL_UPSTREAM"

        graph = TaskGraph(
            execute, storage, RunUsage(), UsageLimits(request_limit=100)
        )
        task = TaskSpec(
            id="child",
            skill_ids=("fixture.skill.norms",),
            prompt="Scoped",
            depends_on=("parent",),
        )
        with self.assertRaisesRegex(ValueError, "dependencies"):
            await graph.run(task)
        await graph.run(
            TaskSpec(
                id="parent",
                skill_ids=("fixture.skill.norms",),
                prompt="Private",
            )
        )
        self.assertEqual((await graph.run(task)).content, "Independent")

    async def test_resume_charges_work_after_the_last_coordinator_checkpoint(
        self,
    ) -> None:
        stored: dict[str, JsonValue] = {}

        async def storage(key: str, value: JsonValue | None) -> JsonValue:
            if value is not None:
                stored[key] = value
            return stored.get(key)

        async def execute(
            spec: TaskSpec,
            inputs: tuple[TaskResult, ...],
            limits: UsageLimits,
            usage: RunUsage,
        ) -> str:
            usage.requests += 2
            return "Done"

        first = TaskGraph(
            execute, storage, RunUsage(), UsageLimits(request_limit=20)
        )
        task = TaskSpec(
            id="saved", skill_ids=("fixture.skill.norms",), prompt="Q"
        )
        await first.run(task)
        usage = RunUsage(requests=1)
        resumed = TaskGraph(
            execute, storage, usage, UsageLimits(request_limit=20)
        )
        await resumed.run(task)
        self.assertEqual(usage.requests, 3)
        checkpoint = resumed.consumption()
        usage_after_checkpoint = RunUsage(requests=3)
        again = TaskGraph(
            execute,
            storage,
            usage_after_checkpoint,
            UsageLimits(request_limit=20),
            prior_consumption=checkpoint,
        )
        await again.run(task)
        self.assertEqual(usage_after_checkpoint.requests, 3)

    async def test_more_than_eight_tasks_are_bounded_and_resumed(self) -> None:
        stored: dict[str, JsonValue] = {}
        active = 0
        peak = 0
        calls = 0

        async def storage(key: str, value: JsonValue | None) -> JsonValue:
            if value is not None:
                stored[key] = value
            return stored.get(key)

        async def execute(
            spec: TaskSpec,
            inputs: tuple[TaskResult, ...],
            limits: UsageLimits,
            usage: RunUsage,
        ) -> str:
            nonlocal active, peak, calls
            calls += 1
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            usage.requests += 1
            active -= 1
            return spec.prompt

        graph = TaskGraph(
            execute,
            storage,
            RunUsage(),
            UsageLimits(request_limit=100),
            concurrency=3,
        )
        tasks = tuple(
            TaskSpec(
                id=f"ground-{i}",
                skill_ids=("fixture.skill.norms",),
                prompt=f"Question {i}",
            )
            for i in range(12)
        )
        await asyncio.gather(*(graph.run(task) for task in tasks))
        self.assertEqual(calls, 12)
        self.assertLessEqual(peak, 3)
        resumed = TaskGraph(
            execute,
            storage,
            RunUsage(),
            UsageLimits(request_limit=100),
            concurrency=3,
        )
        await resumed.run(tasks[0])
        self.assertEqual(calls, 12)
        with self.assertRaisesRegex(ValueError, "changed"):
            await resumed.run(
                tasks[0].model_copy(update={"prompt": "Another question"})
            )

    async def test_sealed_task_rejects_internal_reports_and_free_text(
        self,
    ) -> None:
        async def storage(key: str, value: JsonValue | None) -> JsonValue:
            return None

        async def execute(
            spec: TaskSpec,
            inputs: tuple[TaskResult, ...],
            limits: UsageLimits,
            usage: RunUsage,
        ) -> str:
            return "internal strategy"

        graph = TaskGraph(
            execute, storage, RunUsage(), UsageLimits(request_limit=100)
        )
        await graph.run(
            TaskSpec(
                id="internal",
                skill_ids=("fixture.skill.norms",),
                prompt="Private",
            )
        )
        with self.assertRaisesRegex(ValueError, "draft"):
            await graph.run(
                TaskSpec(
                    id="review",
                    skill_ids=("fixture.skill.defense",),
                    input_ids=("internal",),
                    sealed=True,
                )
            )
        with self.assertRaises(ValueError):
            TaskSpec(
                id="leak",
                skill_ids=("fixture.skill.defense",),
                prompt="strategy",
                input_ids=("draft",),
                sealed=True,
            )

    async def test_interruption_can_replay_research_but_never_reuse_changed_scope(
        self,
    ) -> None:
        stored: dict[str, JsonValue] = {}
        attempts = 0

        async def storage(key: str, value: JsonValue | None) -> JsonValue:
            if value is not None:
                stored[key] = value
            return stored.get(key)

        async def execute(
            spec: TaskSpec,
            inputs: tuple[TaskResult, ...],
            limits: UsageLimits,
            usage: RunUsage,
        ) -> str:
            nonlocal attempts
            attempts += 1
            usage.requests += 1
            if attempts == 1:
                raise asyncio.CancelledError
            return "Recovered"

        task = TaskSpec(
            id="search", skill_ids=("fixture.skill.norms",), prompt="Scope"
        )
        graph = TaskGraph(
            execute, storage, RunUsage(), UsageLimits(request_limit=100)
        )
        with self.assertRaises(asyncio.CancelledError):
            await graph.run(task)
        resumed = TaskGraph(
            execute, storage, RunUsage(), UsageLimits(request_limit=100)
        )
        self.assertEqual((await resumed.run(task)).content, "Recovered")
        self.assertEqual(attempts, 2)

    async def test_request_budget_is_reserved_before_parallel_calls(
        self,
    ) -> None:
        async def storage(key: str, value: JsonValue | None) -> JsonValue:
            return None

        async def execute(
            spec: TaskSpec,
            inputs: tuple[TaskResult, ...],
            limits: UsageLimits,
            usage: RunUsage,
        ) -> str:
            await asyncio.sleep(0)
            usage.requests += 1
            return "Done"

        usage = RunUsage()
        graph = TaskGraph(
            execute,
            storage,
            usage,
            UsageLimits(request_limit=3),
            concurrency=3,
            coordinator_reserve=1,
        )
        outcomes = await asyncio.gather(
            *(
                graph.run(
                    TaskSpec(
                        id=f"n{i}",
                        skill_ids=("fixture.skill.norms",),
                        prompt="Q",
                    )
                )
                for i in range(3)
            ),
            return_exceptions=True,
        )
        self.assertTrue(any(isinstance(item, ValueError) for item in outcomes))
        self.assertLessEqual(usage.requests, 2)


if __name__ == "__main__":
    unittest.main()
