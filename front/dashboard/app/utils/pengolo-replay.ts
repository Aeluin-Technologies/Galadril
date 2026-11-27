import { record, text } from "../../shared/records.ts";
import type {
  EvidenceReference,
  GenerationReplay,
  PengoloEvent,
  PengoloMessage,
  ResponsePart,
} from "./pengolo-api.ts";

function integer(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0)
    throw new Error("Invalid Pengolo replay number");
  return value;
}

function boolean(value: unknown): boolean {
  if (typeof value !== "boolean")
    throw new Error("Invalid Pengolo replay flag");
  return value;
}

function list<T>(value: unknown, parse: (item: unknown) => T): T[] {
  if (!Array.isArray(value))
    throw new Error("Invalid Pengolo replay collection");
  return value.map(parse);
}

function optional<T>(
  value: unknown,
  parse: (item: unknown) => T,
): T | undefined {
  return value == null ? undefined : parse(value);
}

function event(value: unknown): PengoloEvent {
  const { sequence, kind, content } = record(value);
  return {
    sequence: integer(sequence),
    kind: text(kind),
    content: text(content),
  };
}

function part(value: unknown): ResponsePart {
  const data = record(value);
  switch (data.kind) {
    case "markdown":
      return { kind: data.kind, text: text(data.text) };
    case "reference":
      return {
        kind: data.kind,
        resourceType: text(data.resourceType),
        resourceId: text(data.resourceId),
        label: text(data.label),
      };
    case "artifact":
      return {
        kind: data.kind,
        artifactType: text(data.artifactType),
        id: text(data.id),
        title: text(data.title),
      };
    default:
      throw new Error("Invalid Pengolo replay message part");
  }
}

function evidence(value: unknown): EvidenceReference {
  const data = record(value);
  return {
    resourceId: text(data.resourceId),
    resourceType: text(data.resourceType),
    entityId: data.entityId === null ? null : optional(data.entityId, text),
  };
}

function message(value: unknown): PengoloMessage {
  const data = record(value);
  if (data.role !== "user" && data.role !== "assistant")
    throw new Error("Invalid Pengolo replay message role");

  return {
    id: text(data.id),
    role: data.role,
    text: text(data.text),
    fileIds: optional(data.fileIds, (value) => list(value, text)),
    generationId: optional(data.generationId, text),
    status: optional(data.status, text),
    sourceUnavailable: optional(data.sourceUnavailable, boolean),
    emailDrafts: optional(data.emailDrafts, (value) => list(value, text)),
    activityKinds: optional(data.activityKinds, (value) => list(value, text)),
    parts: optional(data.parts, (value) => list(value, part)),
    evidence: optional(data.evidence, (value) => list(value, evidence)),
    toolCheckpoints: optional(data.toolCheckpoints, (value) =>
      list(value, (item) => {
        const checkpoint = record(item);
        return {
          offset: integer(checkpoint.offset),
          operation: text(checkpoint.operation),
        };
      }),
    ),
    taskChecks: optional(data.taskChecks, (value) =>
      list(value, (item) => {
        const check = record(item);
        return { index: integer(check.index), checked: boolean(check.checked) };
      }),
    ),
  };
}

/** Decode the subscription payload before replaying it into cached workspace state. */
export function parseGenerationReplay(value: unknown): GenerationReplay {
  const data = record(value);
  return {
    generationId: text(data.generationId),
    status: text(data.status),
    events: list(data.events, event),
    message: data.message === null ? null : message(data.message),
    chatRevision:
      data.chatRevision === null ? null : integer(data.chatRevision),
    chatUpdatedAt:
      data.chatUpdatedAt === null ? null : integer(data.chatUpdatedAt),
    ...(data.canAnswer === undefined
      ? {}
      : { canAnswer: boolean(data.canAnswer) }),
  };
}
