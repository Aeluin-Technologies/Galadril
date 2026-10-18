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

export interface NavigationHit {
  id: string;
  kind: "navigation" | "action";
  href: string;
  label: string;
  description: string;
  score: number;
}

export function useCommandPaletteSearch() {
  const { locale } = useI18n();
  const { session } = useAccount();
  const searchQuery = ref("");
  const navigation = shallowRef<NavigationHit[]>([]);
  let disposed = false;
  const state = shallowRef<{
    items: SearchHit[];
    after: string | null;
    loading: boolean;
    failed: boolean;
  }>({ items: [], after: null, loading: false, failed: false });
  const pager = createPagedSearch(
    async (query: string, after: string | null, signal: AbortSignal) => {
      const response = await $fetch<{
        data?: {
          globalSearchPage: {
            items: SearchHit[];
            after: string | null;
            navigation?: NavigationHit[];
          };
        };
        errors?: unknown[];
      }>("/graphql", {
        method: "POST",
        credentials: "same-origin",
        signal,
        body: {
          query:
            "query($query: String!, $after: String, $locale: String) { globalSearchPage(query: $query, after: $after, locale: $locale, limit: 20) { items { kind entityId eventId eventType modality createdAtMs eventTimeMs score } navigation { id kind href label description score } after } }",
          variables: { query, after, locale: locale.value },
        },
      });
      if (response.errors?.length || !response.data)
        throw new Error("Search unavailable");
      if (!signal.aborted && !after)
        navigation.value = (
          response.data.globalSearchPage.navigation || []
        ).slice(0, 12);
      return response.data.globalSearchPage;
    },
    (value) => {
      state.value = value;
    },
  );
  const debounced = useDebounceFn((value: string) => {
    if (!disposed && value === searchQuery.value) return pager.search(value);
  }, 300);
  watch(searchQuery, (value) => {
    navigation.value = [];
    pager.prepare(value);
    void debounced(value);
  });
  const invalidate = () => {
    searchQuery.value = "";
    navigation.value = [];
    pager.clear();
  };
  watch(
    () => JSON.stringify([session.value?.tenant, session.value?.subject]),
    invalidate,
  );
  watch(locale, invalidate);
  onMounted(() =>
    window.addEventListener("pengolo-access-invalidated", invalidate),
  );
  onUnmounted(() => {
    disposed = true;
    pager.clear();
    window.removeEventListener("pengolo-access-invalidated", invalidate);
  });
  return {
    searchQuery,
    rawSearchResults: computed(() => state.value.items),
    remoteNavigation: computed(() => navigation.value),
    isLoading: computed(() => state.value.loading),
    hasMore: computed(() => !!state.value.after),
    searchFailed: computed(() => state.value.failed),
    loadMore: () => pager.more(),
  };
}
