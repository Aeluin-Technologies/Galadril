import { required } from "./support.ts";
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  projectActivities,
  parseActivity,
} from "../app/utils/pengolo-activity.ts";

test("billing transitions survive replay without exposing account credentials", () => {
  const billing = { type: "billing", provider: "api", reason: "quota" };
  assert.deepEqual(parseActivity(JSON.stringify(billing)), billing);
  assert.deepEqual(
    projectActivities([
      { sequence: 1, kind: "activity", content: JSON.stringify(billing) },
    ]).billing,
    billing,
  );
  assert.equal(
    parseActivity(JSON.stringify({ ...billing, token: "secret" })),
    null,
  );
  assert.equal(
    parseActivity(JSON.stringify({ ...billing, reason: "arbitrary" })),
    null,
  );
});

test("source warnings survive replay without accepting private error details", () => {
  const warning = { type: "warning", source: "read_file" };
  const events = [1, 2].map((sequence) => ({
    sequence,
    kind: "activity",
    content: JSON.stringify(warning),
  }));
  assert.deepEqual(projectActivities(events).warnings, ["read_file"]);
  assert.equal(
    parseActivity(JSON.stringify({ ...warning, details: "private" })),
    null,
  );
  assert.equal(
    parseActivity(JSON.stringify({ ...warning, source: "arbitrary" })),
    null,
  );
});

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
  assert.equal(required(state.tasks[0]).status, "completed");
  assert.equal(state.questions.length, 1);
  assert.equal(required(state.questions[0]).resolved, true);
  assert.equal(required(state.questions[0]).cancelled, false);
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
test("Python execution replays bounded code, stdout and image fragments in order", () => {
  const event = (sequence: number, channel: string, data: string) => ({
    sequence,
    kind: "activity",
    content: JSON.stringify({ type: "python", id: "run1", channel, data }),
  });
  const state = projectActivities([
    event(3, "output", "42"),
    event(1, "code", "print("),
    event(2, "code", "42)"),
    event(4, "status", "completed"),
  ]);
  assert.equal(state.python[0]?.code, "print(42)");
  assert.equal(state.python[0]?.output, "42");
  assert.equal(state.python[0]?.status, "completed");
  assert.equal(
    parseActivity(
      JSON.stringify({
        type: "python",
        id: "run1",
        channel: "image",
        data: "<script>",
      }),
    ),
    null,
  );
  assert.equal(
    parseActivity(
      JSON.stringify({
        type: "python",
        id: "run1",
        channel: "code",
        data: "x".repeat(257),
      }),
    ),
    null,
  );
});

test("email proposals survive replay without sender authority", () => {
  const draft = {
    type: "email_draft",
    id: "draft_1",
    to: ["to@example.org"],
    cc: [],
    bcc: ["private@example.org"],
    subject: "Hello",
    body: "A draft",
  };
  assert.deepEqual(parseActivity(JSON.stringify(draft)), draft);
  assert.equal(
    parseActivity(JSON.stringify({ ...draft, account: "chosen-by-model" })),
    null,
  );
  assert.deepEqual(
    projectActivities(
      [1, 2].map((sequence) => ({
        sequence,
        kind: "activity",
        content: JSON.stringify(draft),
      })),
    ).emails,
    [draft],
  );
});
