import { invalidatesWorkspace, unavailableActiveChat } from "~/utils/pengolo";
import source from "~/graphql/pengolo.graphql?raw";
import { pengoloDocuments } from "~/utils/pengolo-documents";

const documents = pengoloDocuments(source);
export function pengoloOperation(name: string): string {
  const document = documents.get(name);
  if (!document) throw new Error(`Unknown Pengolo operation: ${name}`);
  return document;
}

export interface WorkspaceRecord<T = Record<string, unknown>> {
  id: string;
  ownerId: string;
  projectId: string | null;
  expiresAt: number | null;
  revision: number;
  updatedAt: number;
  data: T;
}

export interface QuestionResponse {
  questionId: string;
  cancelled: boolean;
  answers: {
    header: string;
    selected: string[];
    customAnswer: string | null;
  }[];
}

export interface PengoloEvent {
  sequence: number;
  kind: string;
  content: string;
}
export interface GraphContext {
  kind: "entity" | "event" | "relation";
  id: string;
}
export type ResponsePart =
  | { kind: "markdown"; text: string }
  | {
      kind: "reference";
      resourceType: string;
      resourceId: string;
      label: string;
    }
  | { kind: "artifact"; artifactType: string; id: string; title: string };
export interface EvidenceReference {
  resourceType: string;
  resourceId: string;
  entityId?: string | null;
}
export interface PengoloMessage {
  parts?: ResponsePart[];
  evidence?: EvidenceReference[];
  id: string;
  role: "user" | "assistant";
  text: string;
  fileIds?: string[];
  generationId?: string;
  status?: string;
  taskChecks?: Array<{ index: number; checked: boolean }>;
}

let requestEpoch = 0;
export function invalidatePengoloRequests(): void {
  requestEpoch++;
}

export async function pengoloData<T>(
  query: string,
  variables: Record<string, unknown> = {},
  allowUnavailableChat = false,
): Promise<T> {
  const epoch = requestEpoch;
  const response = await $fetch<{
    data?: Record<string, unknown>;
    errors?: Array<{
      path?: Array<string | number>;
      extensions?: { code?: string };
    }>;
  }>("/graphql", {
    method: "POST",
    credentials: "same-origin",
    body: { query, variables },
  });
  if (epoch !== requestEpoch) throw new Error("Workspace identity changed");
  if (
    (response.errors?.length &&
      !(allowUnavailableChat && unavailableActiveChat(response.errors))) ||
    !response.data
  ) {
    if (import.meta.client && invalidatesWorkspace(response.errors ?? []))
      window.dispatchEvent(new Event("pengolo-access-invalidated"));
    throw new Error(
      "Pengolo could not complete this request. Your access may have changed.",
    );
  }
  return response.data as T;
}

export async function pengoloRequest<T>(
  query: string,
  variables: Record<string, unknown> = {},
): Promise<T> {
  const data = await pengoloData<Record<string, T>>(query, variables);
  return Object.values(data)[0] as T;
}

export function pengoloMutation<T>(
  field: string,
  input: Record<string, unknown>,
): Promise<T> {
  return pengoloRequest<T>(pengoloOperation(field), { input });
}

export interface ChatData {
  title: string;
  pinned: boolean;
  messages?: PengoloMessage[];
  messagesBefore?: string | null;
  activeGeneration: string | null;
  generationContext?: {
    agentIds?: string[];
    graphContext: GraphContext | null;
    modelAlias: string;
    webSearch: boolean;
  };
}
export interface ProjectData {
  name: string;
  icon: string;
  color: string;
  shared: boolean;
  roles: string[];
  contextMode: "personal" | "project";
}
export interface FileData {
  name: string;
  mime: string;
  size: number;
  status: "uploading" | "ready" | "deleted";
  originalName?: string;
  temporarySession: string | null;
}
export type ChatRecord = WorkspaceRecord<ChatData>;
export interface MessagePage {
  items: PengoloMessage[];
  after: string | null;
}
export type ChatResponse = WorkspaceRecord<
  Omit<ChatData, "messages"> & { messages?: MessagePage }
>;
export function chatRecord(record: ChatResponse): ChatRecord {
  const { messages, ...data } = record.data;
  return {
    ...record,
    data: {
      ...data,
      ...(messages
        ? { messages: messages.items, messagesBefore: messages.after }
        : {}),
    },
  };
}
export interface WorkspaceIdentity {
  version: string;
  webSearchAvailable: boolean;
  administrator: boolean;
}
export interface GenerationReplay {
  canAnswer?: boolean;
  generationId: string;
  status: string;
  events: PengoloEvent[];
  message: PengoloMessage | null;
  chatRevision: number | null;
  chatUpdatedAt: number | null;
}
export type ProjectRecord = WorkspaceRecord<ProjectData>;
export type FileRecord = WorkspaceRecord<FileData>;
export type GenerationRecord = WorkspaceRecord<{
  status: string;
  chatId: string;
  sequence: number;
}>;
