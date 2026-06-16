import { useDebounceFn } from "@vueuse/core";

export interface SearchHit {
  kind: string;
  entityId?: string | null;
  eventId?: string | null;
  eventType?: string | null;
  modality?: string | null;
  createdAtMs?: number | null;
  eventTimeMs?: number | null;
  score?: number | null;
  payload?: any;
  isStaticAction?: boolean;
  isHistoryItem?: boolean;
}

interface GlobalSearchResponse {
  globalSearch: SearchHit[];
}

export function useCommandPaletteSearch() {
  const searchQuery = ref("");
  const rawSearchResults = ref<SearchHit[]>([]);

  const GLOBAL_SEARCH_QUERY = gql`
    query GlobalSearchQuery($query: String!, $limit: Int) {
      globalSearch(query: $query, limit: $limit) {
        kind
        entityId
        eventId
        eventType
        modality
        createdAtMs
        eventTimeMs
        score
        payload
      }
    }
  `;

  const {
    result,
    loading: isLoading,
    load,
  } = useLazyQuery<GlobalSearchResponse>(GLOBAL_SEARCH_QUERY, () => ({
    query: searchQuery.value,
    limit: 15,
  }));

  /**
   * Executes the global GraphQL RAG search query with a specified limit.
   * @param {string} queryText - The raw text query provided by the user.
   * @returns {Promise<void>}
   */
  const executeGlobalSearch = async (queryText: string): Promise<void> => {
    if (!queryText.trim()) {
      rawSearchResults.value = [];
      return;
    }

    try {
      const response = await load(GLOBAL_SEARCH_QUERY, {
        query: queryText,
        limit: 15,
      });
      if (response && "globalSearch" in response) {
        rawSearchResults.value = response.globalSearch || [];
      } else {
        rawSearchResults.value = [];
      }
    } catch (error) {
      console.error("GraphRAG engine lookup failed:", error);
      rawSearchResults.value = [];
    }
  };

  const debouncedSearch = useDebounceFn((val: string) => {
    executeGlobalSearch(val);
  }, 300);

  watch(searchQuery, (newVal) => {
    debouncedSearch(newVal);
  });

  watch(result, (newResult) => {
    if (newResult?.globalSearch) {
      rawSearchResults.value = newResult.globalSearch;
    }
  });

  return {
    searchQuery,
    isLoading,
    rawSearchResults,
  };
}
