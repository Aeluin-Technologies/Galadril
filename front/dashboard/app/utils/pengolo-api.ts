export interface WorkspaceRecord<T = Record<string, unknown>> {
  id: string;
  ownerId: string;
  projectId: string | null;
  expiresAt: number | null;
  revision: number;
  updatedAt: number;
  data: T;
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
  resource_type: string;
  resource_id: string;
  entity_id?: string | null;
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
}

let requestEpoch = 0;
export function invalidatePengoloRequests(): void {
  requestEpoch++;
}

export async function pengoloRequest<T>(
  query: string,
  variables: Record<string, unknown> = {},
): Promise<T> {
  const epoch = requestEpoch;
  const response = await $fetch<{
    data?: Record<string, unknown>;
    errors?: Array<{ extensions?: { code?: string } }>;
  }>("/graphql", {
    method: "POST",
    credentials: "same-origin",
    body: { query, variables },
  });
  if (epoch !== requestEpoch) throw new Error("Workspace identity changed");
  if (response.errors?.length || !response.data) {
    if (import.meta.client)
      window.dispatchEvent(new Event("pengolo-access-invalidated"));
    throw new Error(
      "Pengolo could not complete this request. Your access may have changed.",
    );
  }
  const value = Object.values(response.data)[0];
  return (typeof value === "string" ? JSON.parse(value) : value) as T;
}

export function pengoloMutation<T>(
  field: string,
  input: Record<string, unknown>,
): Promise<T> {
  return pengoloRequest<T>(
    `mutation Pengolo($input: JSON!) { ${field}(input: $input) }`,
    { input: JSON.stringify(input) },
  );
}

export interface ChatData {
  title: string;
  pinned: boolean;
  messages?: PengoloMessage[];
  activeGeneration: string | null;
  generationContext?: {
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
export type ProjectRecord = WorkspaceRecord<ProjectData>;
export type FileRecord = WorkspaceRecord<FileData>;
export type GenerationRecord = WorkspaceRecord<{
  status: string;
  chatId: string;
  sequence: number;
}>;
