import { useDebounceFn } from "@vueuse/core";

export interface SearchHit {
  kind: "entity_state" | "event" | "embedding";
  entity_id?: string;
  event_id?: string;
  event_type?: string;
  modality?: string;
  created_at_ms?: number;
  event_time_ms?: number;
  score: number;
  payload?: any;
  isStaticAction?: boolean;
  isHistoryItem?: boolean;
}

export function useCommandPaletteSearch() {
  const searchQuery = ref("");
  const isLoading = ref(false);
  const rawSearchResults = ref<SearchHit[]>([]);

  const executeGlobalSearch = async (queryText: string): Promise<void> => {
    if (!queryText.trim()) {
      rawSearchResults.value = [];
      return;
    }

    isLoading.value = true;
    try {
      const response = await $fetch<{ data: { global_search: SearchHit[] } }>(
        "/api/graphql",
        {
          method: "POST",
          body: {
            query: `
            query GlobalSearchQuery($query: String!, $limit: Int) {
              global_search(query: $query, limit: $limit) {
                kind
                entity_id
                event_id
                event_type
                modality
                created_at_ms
                event_time_ms
                score
                payload
              }
            }
          `,
            variables: { query: queryText, limit: 15 },
          },
        },
      );

      rawSearchResults.value = response?.data?.global_search || [];
    } catch (error) {
      console.error("GraphRAG engine lookup failed:", error);
      rawSearchResults.value = [];
    } finally {
      isLoading.value = false;
    }
  };

  const debouncedSearch = useDebounceFn((val: string) => {
    executeGlobalSearch(val);
  }, 300);

  watch(searchQuery, (newVal) => {
    debouncedSearch(newVal);
  });

  return {
    searchQuery,
    isLoading,
    rawSearchResults,
  };
}
