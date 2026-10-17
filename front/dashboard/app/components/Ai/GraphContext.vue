<script setup lang="ts">
import { createPagedSearch } from "~/utils/paged-search.mjs";
import { useDebounceFn } from "@vueuse/core";
import { usePengoloUi } from "~/stores/usePengoloUi";
import { pengoloRequest, type GraphContext } from "~/utils/pengolo-api";
const props = defineProps<{ open: boolean }>();
const emit = defineEmits<{ close: [] }>();
const ui = usePengoloUi();
const search = ref("");
const results = ref<GraphContext[]>([]);
const loading = ref(false);
const error = ref("");
interface SearchHit {
  kind: string;
  entityId: string | null;
  eventId: string | null;
}
const after = ref<string | null>(null);
const pager = createPagedSearch(
  async (text: string, cursor: string | null) =>
    pengoloRequest<{ items: SearchHit[]; after: string | null }>(
      "query Context($input: StructuredSearchInput!, $after: String) { structuredSearchPage(input: $input, after: $after) { items { kind entityId eventId } after } }",
      { input: { text, limit: 20 }, after: cursor },
    ),
  (state) => {
    loading.value = state.loading;
    after.value = state.after;
    error.value = state.failed ? "No authorized context could be loaded." : "";
    const unique = new Map<string, GraphContext>();
    for (const hit of state.items) {
      const context: GraphContext | null = hit.entityId
        ? { kind: "entity", id: hit.entityId }
        : hit.eventId
          ? { kind: "event", id: hit.eventId }
          : null;
      if (context) unique.set(`${context.kind}/${context.id}`, context);
    }
    results.value = [...unique.values()];
  },
);
const find = useDebounceFn(
  (value: string) => pager.search(props.open && value.length >= 2 ? value : ""),
  250,
);
watch(search, (value) => {
  pager.clear();
  void find(value);
});
watch(
  () => props.open,
  () => {
    search.value = "";
    pager.clear();
  },
);
onUnmounted(() => pager.clear());
function select(context: GraphContext) {
  ui.graphContext = context;
  emit("close");
}
</script>

<template>
  <AiDialog :open="open" title="Galadril Graph Context" @close="emit('close')">
    <div class="p-form">
      <label
        >Find a Galadril object<input
          v-model="search"
          autofocus
          type="search"
          placeholder="Search an entity, event or business object…"
      /></label>
      <p class="p-muted">
        Pengolo will explore this object and its authorized relationships.
      </p>
      <p v-if="loading" role="status">Searching…</p>
      <p v-if="error" role="alert" class="p-error">{{ error }}</p>
      <ul class="p-context-results">
        <li v-for="result in results" :key="`${result.kind}/${result.id}`">
          <UtilsButton
            variant="secondary"
            type="button"
            class="p-button"
            @click="select(result)"
          >
            <span>{{ result.kind }}</span
            ><strong>{{ result.id }}</strong>
          </UtilsButton>
        </li>
      </ul>
      <UtilsButton
        v-if="after"
        variant="secondary"
        type="button"
        :disabled="loading"
        @click="pager.more()"
        >Load more objects</UtilsButton
      >
      <p
        v-if="search.length >= 2 && !loading && !results.length"
        class="p-muted"
      >
        No visible objects match your search.
      </p>
    </div>
    <template #footer
      ><UtilsButton
        variant="secondary"
        type="button"
        class="p-button"
        @click="
          ui.graphContext = null;
          emit('close');
        "
      >
        Clear context
      </UtilsButton></template
    >
  </AiDialog>
</template>
