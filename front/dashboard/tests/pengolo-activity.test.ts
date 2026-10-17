import assert from "node:assert/strict";
import { test } from "node:test";
import {
  projectActivities,
  parseActivity,
} from "../app/utils/pengolo-activity.ts";

test("plans and human questions survive interleaved text, tools and duplicate replay", () => {
  const question = {
    type: "question",
    id: "q1",
    questions: [
      {
        header: "Sources",
        question: "Which sources?",
        options: [
          { label: "Official", description: null },
          { label: "All public", description: null },
        ],
        multi_select: false,
      },
    ],
  };
  const events = [
    { sequence: 1, kind: "content", content: "Initial explanation" },
    {
      sequence: 2,
      kind: "activity",
      content: JSON.stringify({
        type: "plan",
        operation: "upsert",
        agent_id: null,
        task: {
          id: "research",
          content: "Check sources",
          status: "in_progress",
          active_form: "Checking sources",
          parent_id: null,
        },
      }),
    },
    { sequence: 3, kind: "activity", content: JSON.stringify(question) },
    { sequence: 4, kind: "tool", content: "search" },
    {
      sequence: 5,
      kind: "activity",
      content: JSON.stringify({
        type: "question_resolved",
        id: "q1",
        cancelled: false,
      }),
    },
    {
      sequence: 6,
      kind: "activity",
      content: JSON.stringify({
        type: "plan",
        operation: "upsert",
        agent_id: null,
        task: {
          id: "research",
          content: "Check sources",
          status: "completed",
          active_form: "",
          parent_id: null,
        },
      }),
    },
  ];
  const state = projectActivities([...events, ...events]);
  assert.equal(state.tasks.length, 1);
  assert.equal(state.tasks[0].status, "completed");
  assert.equal(state.questions.length, 1);
  assert.equal(state.questions[0].resolved, true);
  assert.equal(state.questions[0].cancelled, false);
});

test("hidden reasoning, invalid questions and unknown fields never become display activity", () => {
  for (const payload of [
    { type: "phase", phase: "thinking", content: "private reasoning" },
    { type: "question", id: "q1", questions: [] },
    { type: "question_resolved", id: "q1", cancelled: "no" },
    { type: "phase", phase: "unknown" },
  ])
    assert.equal(parseActivity(JSON.stringify(payload)), null);
  assert.equal(parseActivity("not json"), null);
  assert.equal(parseActivity("x".repeat(65537)), null);
});
