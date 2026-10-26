export interface OutputEvent {
  sequence: number;
  kind: string;
  content: string;
}

/** Provider responses must never navigate an OAuth window to an arbitrary origin. */
export function chatgptAuthorizationUrl(value: unknown): string | null {
  if (typeof value !== "string" || value.length > 65536) return null;
  try {
    const url = new URL(value);
    return url.origin === "https://auth.openai.com" &&
      url.pathname === "/api/accounts/authorize" &&
      !url.username &&
      !url.password &&
      !url.hash &&
      url.searchParams.get("response_type") === "code"
      ? url.href
      : null;
  } catch {
    return null;
  }
}

export function mergeMessages<T extends { id: string }>(
  current: readonly T[],
  incoming: readonly T[],
  older = false,
): T[] {
  const items = new Map<string, T>();
  for (const message of older
    ? [...incoming, ...current]
    : [...current, ...incoming])
    items.set(message.id, message);
  return [...items.values()];
}

export function unavailableActiveChat(
  errors: readonly {
    path?: readonly (string | number)[];
    extensions?: { code?: string };
  }[],
): boolean {
  return (
    errors.length > 0 &&
    errors.every(
      (error) =>
        error.path?.[0] === "activeChat" &&
        ["REQUEST_FAILED", "FORBIDDEN"].includes(error.extensions?.code ?? ""),
    )
  );
}

export function invalidatesWorkspace(
  errors: readonly { message?: string; extensions?: { code?: string } }[],
): boolean {
  return errors.some((error) =>
    ["FORBIDDEN", "UNAUTHENTICATED", "AUTHENTICATION_EXPIRED"].includes(
      error.extensions?.code ?? "",
    ),
  );
}
export interface CatalogModel {
  alias: string;
  reasoningEfforts: readonly string[];
}
export interface Draft {
  chatId: string | null;
  projectId: string | null;
  prompt: string;
  files: string[];
  graphContext: { kind: string; id: string } | null;
  temporary: boolean;
  withLibrary: boolean;
}

/** Invalidating a scope also invalidates results that are still in flight. */
/** Estimate JSON residency without allocating another serialized response. */
function cacheValueBytes(value: unknown, budget: number): number {
  let remainingNodes = 4096;
  function measure(value: unknown, depth: number): number {
    if (--remainingNodes < 0 || depth > 16) return Infinity;
    if (typeof value === "string") return value.length * 2;
    if (value === null || typeof value === "boolean") return 4;
    if (typeof value === "number") return 8;
    if (typeof value !== "object") return Infinity;
    let bytes = 24;
    if (Array.isArray(value)) {
      for (const child of value as readonly unknown[]) {
        bytes += 8 + measure(child, depth + 1);
        if (bytes > budget) return Infinity;
      }
    } else {
      if (Object.getPrototypeOf(value) !== Object.prototype) return Infinity;
      for (const key in value) {
        if (!Object.hasOwn(value, key)) continue;
        bytes +=
          24 +
          key.length * 2 +
          measure((value as Record<string, unknown>)[key], depth + 1);
        if (bytes > budget) return Infinity;
      }
    }
    return bytes;
  }
  return measure(value, 0);
}

/** Bounded admission keeps cached pages from retaining an entire workspace. */
export function createRequestCache<T>(
  options: {
    maxEntries?: number;
    maxBytes?: number;
    maxPending?: number;
    now?: () => number;
  } = {},
) {
  const maxEntries = options.maxEntries ?? 32;
  const maxBytes = options.maxBytes ?? 1024 * 1024;
  const maxPending = options.maxPending ?? 16;
  const now = options.now ?? Date.now;
  if (
    ![maxEntries, maxBytes, maxPending].every(
      (limit) => Number.isSafeInteger(limit) && limit > 0,
    )
  )
    throw new RangeError("Invalid request cache budget");
  const values = new Map<
    string,
    { value: T; expires: number; bytes: number }
  >();
  const pending = new Map<string, Promise<T>>();
  let epoch = 0;
  let bytes = 0;
  function remove(key: string) {
    const entry = values.get(key);
    if (entry) bytes -= entry.bytes;
    values.delete(key);
  }
  function purge() {
    const time = now();
    for (const [key, entry] of values) if (entry.expires <= time) remove(key);
  }
  return {
    get(key: string, load: () => Promise<T>, ttl = 30_000): Promise<T> {
      purge();
      if (key.length > 2048)
        return Promise.reject(new Error("Request cache key exceeds limit"));
      const generation = epoch;
      const cached = values.get(key);
      if (cached) {
        values.delete(key);
        values.set(key, cached);
        return Promise.resolve().then(() => {
          if (generation !== epoch)
            throw new Error("Workspace identity changed");
          if (values.get(key) !== cached)
            throw new Error("Workspace request invalidated");
          return cached.value;
        });
      }
      const inflight = pending.get(key);
      if (inflight) return inflight;
      if (pending.size >= maxPending)
        return Promise.reject(new Error("Too many pending workspace requests"));
      const request = load()
        .then((value) => {
          if (generation !== epoch)
            throw new Error("Workspace identity changed");
          if (pending.get(key) !== request)
            throw new Error("Workspace request invalidated");
          purge();
          const weight = 64 + key.length * 2 + cacheValueBytes(value, maxBytes);
          if (Number.isFinite(ttl) && ttl > 0 && weight <= maxBytes) {
            while (values.size >= maxEntries || bytes + weight > maxBytes) {
              const oldest = values.keys().next().value;
              if (oldest === undefined) break;
              remove(oldest);
            }
            values.set(key, {
              value,
              expires: now() + Math.min(ttl, 300_000),
              bytes: weight,
            });
            bytes += weight;
          }
          return value;
        })
        .finally(() => {
          if (pending.get(key) === request) pending.delete(key);
        });
      pending.set(key, request);
      return request;
    },
    clear() {
      epoch++;
      values.clear();
      pending.clear();
      bytes = 0;
    },
    invalidate(key: string) {
      remove(key);
      pending.delete(key);
    },
    stats() {
      purge();
      return { entries: values.size, bytes, pending: pending.size };
    },
  };
}

export function compatibleModels<T extends CatalogModel>(catalog: {
  models: readonly T[];
}): T[] {
  return catalog.models.filter((model) =>
    model.reasoningEfforts.includes("medium"),
  );
}

export function newDraft(): Draft {
  return {
    chatId: null,
    projectId: null,
    prompt: "",
    files: [],
    graphContext: null,
    temporary: false,
    withLibrary: false,
  };
}

export function mergeEvents(
  current: readonly OutputEvent[],
  incoming: readonly OutputEvent[],
): OutputEvent[] {
  const result = current.slice();
  let cursor = result.at(-1)?.sequence ?? 0;
  for (const event of incoming) {
    if (event.sequence <= cursor) continue;
    if (event.sequence !== cursor + 1)
      throw new Error("Generation replay has a gap");
    result.push(event);
    cursor = event.sequence;
  }
  return result;
}

export function isTerminalStatus(status: string): boolean {
  return (
    status === "completed" || status === "failed" || status === "cancelled"
  );
}

/** A terminal output stays authoritative across overlapping replay snapshots. */
export function replayStatus(
  current: string,
  incoming: string,
  events: readonly OutputEvent[],
): string {
  if (isTerminalStatus(current)) return current;
  const last = events.at(-1);
  return last && isTerminalStatus(last.kind) ? last.kind : incoming;
}
export interface AgentSkill {
  id: string;
  kind: string;
  execution: string;
  compatibility: { allowedModels: string[]; requires: string[] };
}

export function compatibleAgent(
  agent: { projectId: string | null; data: { skillIds: string[] } },
  project: string | null,
  model: string,
  catalog: AgentSkill[],
): boolean {
  if (agent.projectId !== null && agent.projectId !== project) return false;
  const selected = new Set(agent.data.skillIds);
  const pending = [...agent.data.skillIds];
  const visited = new Set<string>();
  while (pending.length) {
    const id = pending.pop()!;
    if (visited.has(id)) continue;
    visited.add(id);
    const item = catalog.find((entry) => entry.id === id);
    if (
      !item ||
      item.execution !== "REGISTERED" ||
      (item.compatibility.allowedModels.length > 0 &&
        !item.compatibility.allowedModels.includes(model)) ||
      (item.kind === "SKILL" && !selected.has(id)) ||
      (selected.has(id) && item.kind !== "SKILL")
    )
      return false;
    pending.push(...item.compatibility.requires);
    if (visited.size > 256 || pending.length > 2048) return false;
  }
  return true;
}
export function documentScore(score: number | null): string | null {
  return score !== null && Number.isFinite(score) ? score.toFixed(2) : null;
}
const indexingLabels: Record<string, string> = {
  queued: "Waiting to index",
  reading: "Reading file",
  parsing: "Parsing and chunking",
  extracting: "Extracting facts",
  embedding: "Computing embeddings",
  publishing: "Publishing search index",
  retrying: "Waiting to retry",
  failed: "Indexing interrupted",
  completed: "Document indexed",
};

/** Progress describes processing stages rather than model accuracy or an ETA. */
export function indexingState(data: {
  indexing?: { stage: string; percent: number } | null;
  retrieval?: { status: string; warnings?: readonly string[] } | null;
}): {
  stage: string;
  label: string;
  percent: number;
  active: boolean;
  reasonKey?: string;
} {
  const progress = data.indexing;
  if (
    progress &&
    progress.stage !== "completed" &&
    Object.hasOwn(indexingLabels, progress.stage) &&
    Number.isInteger(progress.percent) &&
    progress.percent >= 0 &&
    progress.percent <= 99
  ) {
    return {
      ...progress,
      label: indexingLabels[progress.stage]!,
      active: progress.stage !== "failed",
      reasonKey:
        progress.stage === "failed" &&
        data.retrieval?.warnings?.includes("graph_extraction_unauthorized")
          ? "pengolo.copy.indexing_credentials_refused"
          : undefined,
    };
  }
  if (
    (!progress || progress.stage === "completed") &&
    data.retrieval?.status === "ready" &&
    !data.retrieval.warnings?.includes("enrichment_pending")
  )
    return {
      stage: "completed",
      label: indexingLabels.completed!,
      percent: 100,
      active: false,
    };
  if (data.retrieval?.status === "failed")
    return {
      stage: "failed",
      label: indexingLabels.failed!,
      percent: 0,
      active: false,
    };
  return {
    stage: "queued",
    label: indexingLabels.queued!,
    percent: 0,
    active: true,
  };
}

/** Display references are narrower than the provenance retained for authorization. */
export function documentMentioned(
  text: string,
  id: string,
  name: string,
): boolean {
  const normalized = text.normalize("NFKC").toLowerCase();
  const escape = (value: string) =>
    value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  if (
    id &&
    new RegExp(
      `(?<![a-z0-9])${escape(id.toLowerCase())}(?![a-z0-9])`,
      "u",
    ).test(normalized)
  )
    return true;
  const filename = name.normalize("NFKC").toLowerCase().trim();
  return (
    !!filename &&
    new RegExp(
      `(?<![\\p{L}\\p{N}_-])${escape(filename)}(?![\\p{L}\\p{N}_-])`,
      "u",
    ).test(normalized)
  );
}

export function conversationModel(
  saved: string | undefined,
  catalog: readonly { alias: string }[],
  current: string,
): string {
  if (saved && catalog.some((model) => model.alias === saved)) return saved;
  if (catalog.some((model) => model.alias === current)) return current;
  return catalog[0]?.alias ?? "";
}
