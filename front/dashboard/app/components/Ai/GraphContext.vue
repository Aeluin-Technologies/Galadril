<script setup lang="ts">
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
const find = useDebounceFn(async () => {
  if (search.value.trim().length < 2) {
    results.value = [];
    return;
  }
  const selected = search.value;
  loading.value = true;
  error.value = "";
  try {
    const hits = await pengoloRequest<SearchHit[]>(
      "query Context($input: StructuredSearchInput!) { structuredSearch(input: $input) { kind entityId eventId } }",
      { input: { text: selected, limit: 15 } },
    );
    if (search.value === selected)
      results.value = hits
        .flatMap((hit): GraphContext[] =>
          hit.entityId
            ? [{ kind: "entity", id: hit.entityId }]
            : hit.eventId
              ? [{ kind: "event", id: hit.eventId }]
              : [],
        )
        .filter(
          (hit, index, all) =>
            all.findIndex(
              (item) => item.kind === hit.kind && item.id === hit.id,
            ) === index,
        );
  } catch {
    error.value = "No authorized context could be loaded.";
  } finally {
    loading.value = false;
  }
}, 250);
watch(search, () => void find());
watch(
  () => props.open,
  () => {
    search.value = "";
    results.value = [];
  },
);
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
