import { hasAsciiControl } from "#shared/text.ts";

export type TaskStatus =
  "pending" | "in_progress" | "completed" | "cancelled" | "blocked";
export interface PlanTask {
  id: string;
  content: string;
  status: TaskStatus;
  active_form: string;
  parent_id: string | null;
}
export interface QuestionOption {
  label: string;
  description: string | null;
}
export interface AgentQuestion {
  header: string;
  question: string;
  options: QuestionOption[];
  multi_select: boolean;
}
export type WarningSource =
  | "search"
  | "graph"
  | "causal"
  | "library"
  | "document_search"
  | "search_evidence"
  | "read_file"
  | "attachment"
  | "web"
  | "run_python"
  | "tool";
const warningSources: readonly WarningSource[] = [
  "search",
  "graph",
  "causal",
  "library",
  "document_search",
  "search_evidence",
  "read_file",
  "attachment",
  "web",
  "run_python",
  "tool",
];
export interface EmailProposal {
  type: "email_draft";
  id: string;
  to: string[];
  cc: string[];
  bcc: string[];
  subject: string;
  body: string;
  reply_source?: { account: string; message: string };
}
export interface WebSite {
  origin: string;
  result_count: number;
}
export interface WebResearchActivity {
  type: "web_research";
  id: string;
  sites: WebSite[];
}
export type AgentActivity =
  | WebResearchActivity
  | EmailProposal
  | {
      type: "billing";
      provider: "chatgpt" | "api";
      reason: "connected" | "not_connected" | "quota" | "model_unavailable";
    }
  | {
      type: "python";
      id: string;
      channel: "code" | "output" | "image" | "status";
      data: string;
    }
  | { type: "warning"; source: WarningSource }
  | {
      type: "phase";
      phase: "thinking" | "writing" | "tool" | "agent" | "waiting";
      agent_id: string | null;
    }
  | {
      type: "plan";
      operation: "upsert" | "delete";
      agent_id: string | null;
      task: PlanTask;
    }
  | { type: "question"; id: string; questions: AgentQuestion[] }
  | { type: "question_resolved"; id: string; cancelled: boolean };
export interface VisibleTask extends PlanTask {
  key: string;
  agentId: string | null;
}
export interface VisibleQuestion {
  id: string;
  questions: AgentQuestion[];
  resolved: boolean;
  cancelled: boolean;
}
export interface ActivityProjection {
  billing: Extract<AgentActivity, { type: "billing" }> | null;
  python: PythonExecution[];
  emails: EmailProposal[];
  warnings: WarningSource[];
  phase: Extract<AgentActivity, { type: "phase" }> | null;
  tasks: VisibleTask[];
  questions: VisibleQuestion[];
}

export interface PythonExecution {
  id: string;
  code: string;
  output: string;
  image: string;
  status: "running" | "completed" | "failed";
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function fields(value: Record<string, unknown>, allowed: string[]): boolean {
  return Object.keys(value).every((key) => allowed.includes(key));
}
function text(value: unknown, maximum: number, minimum = 1): value is string {
  if (typeof value !== "string" || value.length > maximum * 2) return false;
  let count = 0;
  for (const _character of value) if (++count > maximum) return false;
  return count >= minimum;
}
function identifier(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= 128 &&
    /^[A-Za-z0-9_.-]+$/.test(value)
  );
}
function optionalId(value: unknown): boolean {
  return value == null || identifier(value);
}
function taskStatus(value: unknown): value is TaskStatus {
  return (
    typeof value === "string" &&
    ["pending", "in_progress", "completed", "cancelled", "blocked"].includes(
      value,
    )
  );
}
function planTask(value: unknown): PlanTask | null {
  if (
    !record(value) ||
    !fields(value, ["id", "content", "status", "active_form", "parent_id"]) ||
    !identifier(value.id) ||
    !text(value.content, 2048) ||
    !taskStatus(value.status) ||
    !(value.active_form === undefined || text(value.active_form, 2048, 0)) ||
    !optionalId(value.parent_id)
  )
    return null;
  return {
    id: value.id,
    content: value.content,
    status: value.status,
    active_form: (value.active_form as string) ?? "",
    parent_id: (value.parent_id as string) ?? null,
  };
}
function question(value: unknown): AgentQuestion | null {
  if (
    !record(value) ||
    !fields(value, ["header", "question", "options", "multi_select"]) ||
    !text(value.header, 25) ||
    !text(value.question, 500) ||
    !Array.isArray(value.options) ||
    value.options.length < 2 ||
    value.options.length > 6 ||
    !(
      value.multi_select === undefined ||
      typeof value.multi_select === "boolean"
    )
  )
    return null;
  const options: QuestionOption[] = [];
  for (const option of value.options) {
    if (
      !record(option) ||
      !fields(option, ["label", "description"]) ||
      !text(option.label, 50) ||
      !(option.description == null || text(option.description, 200, 0))
    )
      return null;
    options.push({
      label: option.label,
      description: (option.description as string) ?? null,
    });
  }
  if (new Set(options.map((option) => option.label)).size !== options.length)
    return null;
  return {
    header: value.header,
    question: value.question,
    options,
    multi_select: value.multi_select === true,
  };
}

function replySource(value: unknown): EmailProposal["reply_source"] | null {
  if (value == null) return undefined;
  if (!record(value) || !fields(value, ["account", "message"])) return null;
  const { account, message } = value;
  if (typeof account !== "string" || typeof message !== "string") return null;
  if (!/^[a-f0-9]{32}$/.test(account) || !/^[a-f0-9]{32}$/.test(message))
    return null;
  return { account, message };
}

export function parseActivity(content: string): AgentActivity | null {
  if (
    content.length > 65536 ||
    new TextEncoder().encode(content).byteLength > 65536
  )
    return null;
  let value: unknown;
  try {
    value = JSON.parse(content);
  } catch {
    return null;
  }
  if (!record(value)) return null;
  switch (value.type) {
    case "web_research": {
      if (
        !fields(value, ["type", "id", "sites"]) ||
        !identifier(value.id) ||
        !Array.isArray(value.sites) ||
        value.sites.length < 1 ||
        value.sites.length > 8
      )
        return null;
      const sites: WebSite[] = [];
      for (const site of value.sites) {
        if (
          !record(site) ||
          !fields(site, ["origin", "result_count"]) ||
          typeof site.origin !== "string" ||
          site.origin.length > 300 ||
          !/^https?:\/\/(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}$/.test(
            site.origin,
          ) ||
          /\.(?:local|localhost|internal|invalid|test|lan|home|localdomain|example)$/.test(
            site.origin,
          ) ||
          typeof site.result_count !== "number" ||
          !Number.isInteger(site.result_count) ||
          site.result_count < 1 ||
          site.result_count > 8
        )
          return null;
        sites.push({ origin: site.origin, result_count: site.result_count });
      }
      if (
        new Set(sites.map((site) => site.origin)).size !== sites.length ||
        sites.reduce((total, site) => total + site.result_count, 0) > 8
      )
        return null;
      return { type: "web_research", id: value.id, sites };
    }
    case "email_draft": {
      if (
        !fields(value, [
          "type",
          "id",
          "to",
          "cc",
          "bcc",
          "subject",
          "body",
          "reply_source",
        ]) ||
        !identifier(value.id) ||
        !text(value.subject, 998) ||
        hasAsciiControl(value.subject) ||
        !text(value.body, 16384)
      )
        return null;
      const recipients = (raw: unknown): string[] | null =>
        Array.isArray(raw) &&
        raw.length <= 20 &&
        raw.every(
          (item) =>
            typeof item === "string" &&
            item.length <= 320 &&
            item.includes("@") &&
            !hasAsciiControl(item),
        )
          ? raw
          : null;
      const to = recipients(value.to),
        cc = recipients(value.cc),
        bcc = recipients(value.bcc);
      if (!to?.length || !cc || !bcc) return null;
      const source = replySource(value.reply_source);
      if (source === null) return null;
      return {
        type: "email_draft",
        id: value.id,
        to,
        cc,
        bcc,
        subject: value.subject,
        body: value.body,
        ...(source ? { reply_source: source } : {}),
      };
    }
    case "billing":
      if (
        !fields(value, ["type", "provider", "reason"]) ||
        !["chatgpt", "api"].includes(String(value.provider)) ||
        !["connected", "not_connected", "quota", "model_unavailable"].includes(
          String(value.reason),
        )
      )
        return null;
      return {
        type: "billing",
        provider: value.provider as "chatgpt" | "api",
        reason: value.reason as
          "connected" | "not_connected" | "quota" | "model_unavailable",
      };
    case "python": {
      if (
        !fields(value, ["type", "id", "channel", "data"]) ||
        !identifier(value.id) ||
        typeof value.data !== "string"
      )
        return null;
      const valid =
        value.channel === "code" || value.channel === "output"
          ? text(value.data, 256, 0)
          : value.channel === "image"
            ? value.data.length <= 2048 &&
              /^[A-Za-z0-9+/]*={0,2}$/.test(value.data)
            : value.channel === "status" &&
              ["running", "completed", "failed"].includes(value.data);
      return valid
        ? {
            type: "python",
            id: value.id,
            channel: value.channel as "code" | "output" | "image" | "status",
            data: value.data,
          }
        : null;
    }
    case "warning":
      if (
        !fields(value, ["type", "source"]) ||
        typeof value.source !== "string" ||
        !warningSources.includes(value.source as WarningSource)
      )
        return null;
      return { type: "warning", source: value.source as WarningSource };
    case "phase":
      if (
        !fields(value, ["type", "phase", "agent_id"]) ||
        typeof value.phase !== "string" ||
        !["thinking", "writing", "tool", "agent", "waiting"].includes(
          value.phase,
        ) ||
        !optionalId(value.agent_id)
      )
        return null;
      return {
        type: "phase",
        phase: value.phase as Extract<
          AgentActivity,
          { type: "phase" }
        >["phase"],
        agent_id: (value.agent_id as string) ?? null,
      };
    case "plan": {
      if (
        !fields(value, ["type", "operation", "agent_id", "task"]) ||
        (value.operation !== "upsert" && value.operation !== "delete") ||
        !optionalId(value.agent_id)
      )
        return null;
      const task = planTask(value.task);
      return task
        ? {
            type: "plan",
            operation: value.operation as "upsert" | "delete",
            agent_id: (value.agent_id as string) ?? null,
            task,
          }
        : null;
    }
    case "question": {
      if (
        !fields(value, ["type", "id", "questions"]) ||
        !identifier(value.id) ||
        !Array.isArray(value.questions) ||
        value.questions.length < 1 ||
        value.questions.length > 10
      )
        return null;
      const questions: AgentQuestion[] = [];
      for (const item of value.questions) {
        const parsed = question(item);
        if (!parsed) return null;
        questions.push(parsed);
      }
      if (
        new Set(questions.map((item) => item.header)).size !== questions.length
      )
        return null;
      return { type: "question", id: value.id, questions };
    }
    case "question_resolved":
      if (
        !fields(value, ["type", "id", "cancelled"]) ||
        !identifier(value.id) ||
        typeof value.cancelled !== "boolean"
      )
        return null;
      return {
        type: "question_resolved",
        id: value.id,
        cancelled: value.cancelled,
      };
    default:
      return null;
  }
}

export function projectActivities(
  events: readonly { sequence: number; kind: string; content: string }[],
): ActivityProjection {
  const tasks = new Map<string, VisibleTask>();
  const questions = new Map<string, VisibleQuestion>();
  const resolved = new Map<string, boolean>();
  const emails = new Map<string, EmailProposal>();
  const warnings = new Set<WarningSource>();
  const python = new Map<string, PythonExecution>();
  let phase: ActivityProjection["phase"] = null;
  let billing: ActivityProjection["billing"] = null;
  const ordered = [...events]
    .filter((event) => event.kind === "activity")
    .sort((a, b) => a.sequence - b.sequence);
  const seen = new Set<number>();
  for (const event of ordered) {
    if (seen.has(event.sequence)) continue;
    seen.add(event.sequence);
    const activity = parseActivity(event.content);
    if (!activity) continue;
    if (activity.type === "web_research") continue;
    if (activity.type === "email_draft") {
      if (emails.size < 8 && !emails.has(activity.id))
        emails.set(activity.id, activity);
      continue;
    }
    if (activity.type === "python") {
      let execution = python.get(activity.id);
      if (!execution) {
        if (python.size >= 32) continue;
        execution = {
          id: activity.id,
          code: "",
          output: "",
          image: "",
          status: "running",
        };
        python.set(activity.id, execution);
      }
      if (activity.channel === "status")
        execution.status = activity.data as PythonExecution["status"];
      else {
        const maximum =
          activity.channel === "code"
            ? 16384
            : activity.channel === "image"
              ? 174764
              : 131072;
        if (
          execution[activity.channel].length + activity.data.length <=
          maximum
        )
          execution[activity.channel] += activity.data;
        else execution.status = "failed";
      }
    } else if (activity.type === "billing") billing = activity;
    else if (activity.type === "warning") warnings.add(activity.source);
    else if (activity.type === "phase") phase = activity;
    else if (activity.type === "plan") {
      const key = `${activity.agent_id ?? "orchestrator"}:${activity.task.id}`;
      if (activity.operation === "delete") tasks.delete(key);
      else if (tasks.has(key) || tasks.size < 64)
        tasks.set(key, { ...activity.task, key, agentId: activity.agent_id });
    } else if (activity.type === "question") {
      if (questions.has(activity.id) || questions.size < 32)
        questions.set(activity.id, {
          id: activity.id,
          questions: activity.questions,
          resolved: resolved.has(activity.id),
          cancelled: resolved.get(activity.id) ?? false,
        });
    } else {
      if (resolved.has(activity.id) || resolved.size < 32)
        resolved.set(activity.id, activity.cancelled);
      const pending = questions.get(activity.id);
      if (pending)
        questions.set(activity.id, {
          ...pending,
          resolved: true,
          cancelled: activity.cancelled,
        });
    }
  }
  return {
    billing,
    python: [...python.values()],
    emails: [...emails.values()],
    warnings: [...warnings],
    phase,
    tasks: [...tasks.values()],
    questions: [...questions.values()],
  };
}

/** Replay may repeat a tool result; only distinct searches contribute site counts. */
export function webResearchSites(
  events: readonly { kind: string; content: string }[],
): WebSite[] {
  const searches = new Set<string>();
  const sites = new Map<string, WebSite>();
  for (const event of events) {
    if (event.kind !== "activity") continue;
    const activity = parseActivity(event.content);
    if (
      activity?.type !== "web_research" ||
      searches.has(activity.id) ||
      searches.size >= 32
    )
      continue;
    searches.add(activity.id);
    for (const site of activity.sites) {
      const host = new URL(site.origin).hostname;
      const previous = sites.get(host);
      if (previous) previous.result_count += site.result_count;
      else sites.set(host, { ...site });
    }
  }
  return [...sites.values()].sort(
    (a, b) =>
      b.result_count - a.result_count || a.origin.localeCompare(b.origin),
  );
}
