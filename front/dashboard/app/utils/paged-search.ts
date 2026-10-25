/**
 * Keep continuations local to one query and discard replies superseded by input or access changes.
 */
export interface SearchState<T> {
  items: T[];
  after: string | null;
  loading: boolean;
  failed: boolean;
}

/**
 * Discard superseded requests before publishing results or retaining pagination cursors.
 *
 * @param fetchPage - Must honor the supplied abort signal and return at most twenty results.
 * @param publish - Receives complete state snapshots for loading, failure and access changes.
 */
export function createPagedSearch<T>(
  fetchPage: (
    query: string,
    after: string | null,
    signal: AbortSignal,
  ) => Promise<{ items: T[]; after: string | null }>,
  publish: (state: SearchState<T>) => void,
) {
  let epoch = 0;
  let query = "";
  let controller: AbortController | undefined;
  let state: SearchState<T> = {
    items: [],
    after: null,
    loading: false,
    failed: false,
  };
  function clear() {
    controller?.abort();
    controller = undefined;
    epoch++;
    query = "";
    state = { items: [], after: null, loading: false, failed: false };
    publish(state);
  }
  function prepare(value: string) {
    clear();
    query = value.trim();
    if (query.length > 512) {
      state = { ...state, failed: true };
    } else if (query) {
      state = { ...state, loading: true };
    }
    publish(state);
  }
  async function request(append: boolean) {
    if (append && (state.loading || !state.after)) return;
    const selected = ++epoch;
    const previous = append ? state.items : [];
    const after = append ? state.after : null;
    controller?.abort();
    controller = new AbortController();
    state = { items: previous, after, loading: true, failed: false };
    publish(state);
    try {
      const page = await fetchPage(query, after, controller.signal);
      if (epoch !== selected) return;
      if (page.items.length > 20 || (after && page.after === after))
        throw new Error("Invalid search page");
      const items = [...previous, ...page.items].slice(0, 100);
      state = {
        items,
        after: items.length < 100 ? page.after : null,
        loading: false,
        failed: false,
      };
    } catch {
      if (epoch !== selected) return;
      state = { items: [], after: null, loading: false, failed: true };
    }
    publish(state);
  }
  return {
    clear,
    prepare,
    search(value: string) {
      prepare(value);
      return query && !state.failed ? request(false) : Promise.resolve();
    },
    more() {
      return request(true);
    },
  };
}
