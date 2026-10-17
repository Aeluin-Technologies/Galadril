import { useDebounceFn } from "@vueuse/core";
import { createPagedSearch } from "~/utils/paged-search.mjs";

export interface SearchHit {
  kind: string;
  entityId?: string | null;
  eventId?: string | null;
  eventType?: string | null;
  modality?: string | null;
  createdAtMs?: number | null;
  eventTimeMs?: number | null;
  score?: number | null;
}

export function useCommandPaletteSearch() {
  const searchQuery = ref("");
  const state = shallowRef<{
    items: SearchHit[];
    after: string | null;
    loading: boolean;
    failed: boolean;
  }>({ items: [], after: null, loading: false, failed: false });
  const pager = createPagedSearch(
    async (query: string, after: string | null) => {
      const response = await $fetch<{
        data?: {
          globalSearchPage: { items: SearchHit[]; after: string | null };
        };
        errors?: unknown[];
      }>("/graphql", {
        method: "POST",
        credentials: "same-origin",
        body: {
          query:
            "query($query: String!, $after: String) { globalSearchPage(query: $query, after: $after, limit: 20) { items { kind entityId eventId eventType modality createdAtMs eventTimeMs score } after } }",
          variables: { query, after },
        },
      });
      if (response.errors?.length || !response.data)
        throw new Error("Search unavailable");
      return response.data.globalSearchPage;
    },
    (value) => {
      state.value = value;
    },
  );
  const debounced = useDebounceFn((value: string) => pager.search(value), 300);
  watch(searchQuery, (value) => {
    pager.clear();
    void debounced(value);
  });
  const invalidate = () => pager.clear();
  onMounted(() =>
    window.addEventListener("pengolo-access-invalidated", invalidate),
  );
  onUnmounted(() => {
    pager.clear();
    window.removeEventListener("pengolo-access-invalidated", invalidate);
  });
  return {
    searchQuery,
    rawSearchResults: computed(() => state.value.items),
    isLoading: computed(() => state.value.loading),
    hasMore: computed(() => !!state.value.after),
    searchFailed: computed(() => state.value.failed),
    loadMore: () => pager.more(),
  };
}
