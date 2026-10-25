import { object, text, number, flag } from "./email.ts";

export interface EmailThreadMessage {
  root: string;
  message_id: string;
  parent: string;
  subject: string;
  date: string;
  source_key: string;
  model: string;
  analysis: string;
  number: number;
  reference: string;
  received_id: string | null;
  draft_id: string | null;
  ingested: boolean;
  status: string;
}
export interface EmailThread {
  thread_id: string;
  messages: EmailThreadMessage[];
  truncated: boolean;
  indexed_complete: boolean;
}
export interface EmailExecution {
  commandId: string;
  pipelineId: string;
  step: string;
  status: string;
  correlationId: string;
  sourceEmailAccountId?: string | null;
  sourceEmailId?: string | null;
  sourceStoragePath?: string | null;
  result?: unknown;
}
function identifier(value: unknown): string | null {
  if (value === null) return null;
  const id = text(value);
  if (!/^[a-f0-9]{32}$/.test(id)) throw new Error("EMAIL_THREAD_INVALID");
  return id;
}
export function parseEmailThread(value: unknown): EmailThread {
  const data = object(value);
  if (!Array.isArray(data.messages) || data.messages.length > 128)
    throw new Error("EMAIL_THREAD_INVALID");
  const seen = new Set<number>();
  const messages = data.messages.map((value) => {
    const row = object(value),
      sequence = number(row.number);
    if (!Number.isSafeInteger(sequence) || sequence < 1 || seen.has(sequence))
      throw new Error("EMAIL_THREAD_INVALID");
    seen.add(sequence);
    return {
      root: text(row.root),
      message_id: text(row.message_id),
      parent: text(row.parent),
      subject: text(row.subject),
      date: text(row.date),
      source_key: text(row.source_key),
      model: text(row.model),
      analysis: text(row.analysis),
      number: sequence,
      reference: text(row.reference),
      received_id: identifier(row.received_id),
      draft_id: identifier(row.draft_id),
      ingested: flag(row.ingested),
      status: text(row.status),
    };
  });
  return {
    thread_id: text(data.thread_id),
    messages,
    truncated: flag(data.truncated),
    indexed_complete: flag(data.indexed_complete),
  };
}
function sourceKey(value: unknown): string | undefined {
  if (typeof value !== "string") return;
  if (!value.includes("://")) return value;
  try {
    const url = new URL(value);
    return url.protocol === "s3:" ? url.pathname.slice(1) : undefined;
  } catch {
    return;
  }
}
/** Associates only native record provenance; message contents cannot invent graph links. */
export function relatedEmailExecutions(
  rows: readonly EmailExecution[],
  account: string,
  messages: readonly EmailThreadMessage[],
): EmailExecution[] {
  const received = new Set(
    messages.flatMap((message) =>
      message.received_id ? [message.received_id] : [],
    ),
  );
  const sources = new Set(
    messages.map((message) => message.source_key).filter(Boolean),
  );
  const matching = new Set<string>(),
    correlations = new Set<string>();
  for (const execution of rows.slice(0, 100)) {
    const path = sourceKey(execution.sourceStoragePath);
    if (
      (execution.sourceEmailAccountId === account &&
        typeof execution.sourceEmailId === "string" &&
        received.has(execution.sourceEmailId)) ||
      (path && sources.has(path))
    ) {
      matching.add(execution.commandId);
      if (execution.correlationId) correlations.add(execution.correlationId);
    }
  }
  return rows
    .slice(0, 100)
    .filter(
      (row) =>
        matching.has(row.commandId) ||
        (!!row.correlationId && correlations.has(row.correlationId)),
    );
}
