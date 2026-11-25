<script setup lang="ts">
import type { GolSummary } from "~/types/ontology";
const api = useOntologyCatalog(),
  { session } = useAccount();
const items = shallowRef<GolSummary[]>([]),
  search = ref("");
const busy = ref(false),
  failed = ref(false),
  canCreate = ref(false);
let epoch = 0;
const filtered = computed(() =>
  items.value.filter((item) =>
    `${item.displayName} ${item.ontologyId}`
      .toLocaleLowerCase()
      .includes(search.value.trim().toLocaleLowerCase()),
  ),
);
function clear() {
  epoch++;
  api.cancel();
  items.value = [];
  canCreate.value = false;
  busy.value = false;
}
async function load() {
  const request = ++epoch;
  busy.value = true;
  failed.value = false;
  try {
    const [values, allowed] = await Promise.all([
      api.browse(),
      api.canManage().catch(() => false),
    ]);
    if (request !== epoch) return;
    items.value = values;
    canCreate.value = allowed;
  } catch {
    if (request === epoch) {
      items.value = [];
      failed.value = true;
    }
  } finally {
    if (request === epoch) busy.value = false;
  }
}
const invalidate = () => {
  clear();
  void load();
};
onActivated(() => {
  void load();
});
onDeactivated(clear);
onBeforeUnmount(clear);
watch(
  () => JSON.stringify([session.value?.tenant, session.value?.subject]),
  invalidate,
);
onMounted(() =>
  window.addEventListener("pengolo-access-invalidated", invalidate),
);
onBeforeUnmount(() =>
  window.removeEventListener("pengolo-access-invalidated", invalidate),
);
</script>
<template>
  <section class="flex h-full min-h-0 flex-col overflow-hidden bg-canvas">
    <header
      class="shrink-0 border-b border-border bg-surface px-5 py-4 sm:px-8"
    >
      <UiPageHeader
        :title="$t('gol.catalog')"
        :description="$t('gol.catalogHelp')"
        ><template #actions
          ><NuxtLink v-if="canCreate" to="/ontology/new"
            ><UiButton>{{ $t("gol.newOntology") }}</UiButton></NuxtLink
          ></template
        ></UiPageHeader
      >
    </header>
    <div class="min-h-0 flex-1 overflow-auto p-5 sm:p-8">
      <div class="mx-auto max-w-6xl space-y-5">
        <p v-if="busy" role="status" class="text-sm text-muted">
          {{ $t("gol.loading") }}
        </p>
        <UiAlert v-if="failed" tone="danger"
          >{{ $t("gol.unavailable")
          }}<UiButton variant="ghost" :action="load">{{
            $t("gol.retry")
          }}</UiButton></UiAlert
        >
        <UiInput v-model="search" :label="$t('gol.search')" />
        <ul
          v-if="filtered.length"
          class="divide-y divide-border rounded-panel border border-border bg-surface"
        >
          <li
            v-for="item in filtered"
            :key="item.ontologyId"
            class="flex flex-wrap items-center justify-between gap-4 p-4"
          >
            <div>
              <NuxtLink
                :to="`/ontology/edit/${encodeURIComponent(item.ontologyId)}`"
                class="font-medium text-primary-foreground"
                >{{ item.displayName }}</NuxtLink
              >
              <p class="mt-1 text-xs text-muted">{{ item.ontologyId }}</p>
            </div>
            <UiBadge :tone="item.activeGeneration ? 'success' : 'neutral'">{{
              $t(
                item.activeGeneration ? "gol.publishedState" : "gol.draftState",
              )
            }}</UiBadge>
          </li>
        </ul>
        <UiEmptyState
          v-else-if="!busy && !failed"
          :title="$t('gol.empty')"
          :description="$t('gol.catalogHelp')"
        />
      </div>
    </div>
  </section>
</template>
