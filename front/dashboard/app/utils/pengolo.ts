export interface OutputEvent {
  sequence: number;
  kind: string;
  content: string;
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
export function createRequestCache<T>() {
  const values = new Map<string, { value: T; expires: number }>();
  const pending = new Map<string, Promise<T>>();
  let epoch = 0;
  return {
    get(key: string, load: () => Promise<T>, ttl = 30_000): Promise<T> {
      const generation = epoch;
      const cached = values.get(key);
      if (cached && cached.expires > Date.now())
        return Promise.resolve().then(() => {
          if (generation !== epoch)
            throw new Error("Workspace identity changed");
          return cached.value;
        });
      const inflight = pending.get(key);
      if (inflight) return inflight;
      const request = load()
        .then((value) => {
          if (generation !== epoch)
            throw new Error("Workspace identity changed");
          values.set(key, { value, expires: Date.now() + ttl });
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
    },
    invalidate(key: string) {
      values.delete(key);
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

export function publicWebQuery(enabled: boolean, query: string): string | null {
  if (!enabled) return null;
  const text = query.trim();
  if (!text || text.length > 512)
    throw new Error("Enter a public search query (up to 512 characters)");
  return text;
}
