/**
 * Keep continuations local to one query and discard replies superseded by input or access changes.
 * @template T
 * @param {(query: string, after: string | null) => Promise<{items: T[], after: string | null}>} fetchPage
 * @param {(state: {items: T[], after: string | null, loading: boolean, failed: boolean}) => void} publish
 */
export function createPagedSearch(fetchPage, publish) {
  let epoch = 0;
  let query = "";
  /** @type {{items: T[], after: string | null, loading: boolean, failed: boolean}} */
  let state = { items: [], after: null, loading: false, failed: false };
  function clear() {
    epoch++;
    query = "";
    state = { items: [], after: null, loading: false, failed: false };
    publish(state);
  }
  async function request(append) {
    if (append && (state.loading || !state.after)) return;
    const selected = ++epoch;
    const previous = append ? state.items : [];
    const after = append ? state.after : null;
    state = { items: previous, after, loading: true, failed: false };
    publish(state);
    try {
      const page = await fetchPage(query, after);
      if (epoch !== selected) return;
      state = {
        items: [...previous, ...page.items],
        after: page.after,
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
    search(value) {
      query = value.trim();
      return query ? request(false) : (clear(), Promise.resolve());
    },
    more() {
      return request(true);
    },
  };
}
