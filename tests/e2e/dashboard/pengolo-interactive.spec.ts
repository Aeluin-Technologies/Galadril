import { test, expect, finishLogin } from "./fixture.ts";

test("Pengolo interactive planning accepts an answer and continues tools after text", async ({
  page,
}) => {
  const chatId = "a".repeat(32);
  const generationId = "b".repeat(32);
  const identity = {
    version: "interactive",
    webSearchAvailable: true,
    administrator: false,
  };
  const userMessage = {
    id: "user",
    role: "user",
    text: "Watch public developments",
    generationId,
  };
  const chat = {
    id: chatId,
    ownerId: "browser-pengolo",
    projectId: null,
    expiresAt: null,
    revision: 1,
    updatedAt: 1,
    data: {
      title: "Public watch",
      pinned: false,
      activeGeneration: generationId,
      messages: [userMessage],
    },
  };
  const activity = (sequence: number, value: unknown) => ({
    sequence,
    kind: "activity",
    content: JSON.stringify(value),
  });
  const initial = [
    activity(1, { type: "phase", phase: "thinking", agent_id: null }),
    { sequence: 2, kind: "content", content: "Investigation started. " },
    activity(3, {
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
    activity(4, {
      type: "question",
      id: "q1",
      questions: [
        {
          header: "Sources",
          question: "Which sources should I use?",
          options: [
            { label: "Official", description: "Public institutional sources" },
            { label: "All public", description: null },
          ],
          multi_select: false,
        },
      ],
    }),
  ];
  const continued = [
    ...initial,
    activity(5, { type: "question_resolved", id: "q1", cancelled: false }),
    activity(6, { type: "phase", phase: "tool", agent_id: null }),
    { sequence: 7, kind: "tool", content: "search" },
    activity(8, {
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
    { sequence: 9, kind: "content", content: "Official sources checked." },
  ];
  let answered = false;
  let completed = false;
  const channel: { push: () => void } = { push: () => {} };
  const replay = () => ({
    generationId,
    status: completed ? "completed" : "running",
    canAnswer: !answered,
    events: completed
      ? [...continued, { sequence: 10, kind: "completed", content: "" }]
      : answered
        ? continued
        : initial,
    message: completed
      ? {
          id: "final",
          role: "assistant",
          text: "Investigation started. Official sources checked.",
          generationId,
          status: "completed",
        }
      : null,
    chatRevision: completed ? 2 : null,
    chatUpdatedAt: completed ? 2 : null,
  });
  await page.routeWebSocket("**/graphql", (socket) =>
    socket.onMessage((raw) => {
      const frame = JSON.parse(String(raw)) as {
        type: string;
        id?: string;
        payload?: { query?: string };
      };
      if (frame.type === "connection_init")
        socket.send(JSON.stringify({ type: "connection_ack" }));
      if (frame.type !== "subscribe" || !frame.id) return;
      if (frame.payload?.query?.includes("pengoloIdentityChanges"))
        socket.send(
          JSON.stringify({
            type: "next",
            id: frame.id,
            payload: { data: { pengoloIdentityChanges: identity } },
          }),
        );
      if (frame.payload?.query?.includes("pengoloStream")) {
        channel.push = () =>
          socket.send(
            JSON.stringify({
              type: "next",
              id: frame.id,
              payload: { data: { pengoloStream: replay() } },
            }),
          );
        channel.push();
      }
    }),
  );
  await page.route("**/graphql", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    const body = route.request().postDataJSON() as {
      query: string;
      variables?: { input?: Record<string, unknown> };
    };
    if (body.query.includes("query Workspace"))
      return route.fulfill({
        json: {
          data: {
            pengoloIdentity: identity,
            pengoloModels: {
              defaultModel: "fixture",
              models: [
                {
                  alias: "fixture",
                  model: "fixture",
                  provider: "fixture",
                  reasoningEfforts: ["medium"],
                },
              ],
            },
            pengoloProjects: [],
            pengoloChats: { items: [], after: null },
          },
        },
      });
    if (body.query.includes("mutation sendPengolo"))
      return route.fulfill({
        json: {
          data: {
            sendPengolo: {
              chat,
              generation: {
                id: generationId,
                ownerId: "browser-pengolo",
                projectId: null,
                expiresAt: null,
                revision: 1,
                updatedAt: 1,
                data: { status: "running", chatId, sequence: 0 },
              },
              userMessage,
            },
          },
        },
      });
    if (body.query.includes("mutation answerPengoloQuestion")) {
      expect(body.variables?.input).toEqual(
        expect.objectContaining({
          id: generationId,
          questionId: "q1",
          answers: [
            { header: "Sources", selected: ["Official"], customAnswer: null },
          ],
          cancelled: false,
        }),
      );
      expect(body.variables?.input).not.toHaveProperty("capability");
      answered = true;
      return route.fulfill({ json: { data: { answerPengoloQuestion: true } } });
    }
    if (body.query.includes("query Events"))
      return route.fulfill({ json: { data: { pengoloEvents: replay() } } });
    return route.continue();
  });
  await finishLogin(page, "browser-pengolo");
  await page.goto("/pengolo");
  await expect(page.getByLabel("Model", { exact: true })).toHaveValue(
    "fixture",
  );
  await page
    .getByLabel("Message Pengolo", { exact: true })
    .fill("Watch public developments");
  await page.getByRole("button", { name: "Send message", exact: true }).click();
  await expect(
    page.getByText("Investigation started.", { exact: false }),
  ).toBeVisible();
  const plan = page.getByRole("region", { name: "Execution plan" });
  await expect(
    plan.getByText("Checking sources", { exact: true }),
  ).toBeVisible();
  await page.emulateMedia({ reducedMotion: "reduce" });
  await expect
    .poll(() =>
      plan
        .locator(".p-agent-spin")
        .evaluate((element) => getComputedStyle(element).animationName),
    )
    .toBe("none");
  await expect(
    page.getByRole("button", { name: "Send answer", exact: true }),
  ).toBeDisabled();
  await page.getByRole("radio", { name: /Official/ }).check();
  await page.getByRole("button", { name: "Send answer", exact: true }).click();
  await expect(
    page.getByText("Answer received.", { exact: true }),
  ).toBeVisible();
  await expect(
    plan.getByRole("listitem", { name: "Check sources: completed", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Official sources checked.", { exact: false }),
  ).toBeVisible();
  completed = true;
  channel.push();
  await expect(page.getByText("Work completed", { exact: true })).toBeVisible();
  await expect(plan.getByText("Check sources", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Send answer", exact: true }),
  ).toHaveCount(0);
});
