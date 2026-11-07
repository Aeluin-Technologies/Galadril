<script setup lang="ts">
import type { TenantOntology, SemanticPackage } from "~/types/ontology";
const api = useOntologyCatalog();
const { packageLabel } = useOfficialOntologyLabels();
const { session } = useAccount();
const items = shallowRef<TenantOntology[]>([]),
  packages = shallowRef<SemanticPackage[]>([]);
const search = ref(""),
  busy = ref(false),
  failed = ref(false),
  packagesFailed = ref(false),
  canCreate = ref(false);
const filtered = computed(() =>
  items.value.filter((item) =>
    `${item.displayName} ${item.ontologyId}`
      .toLocaleLowerCase()
      .includes(search.value.trim().toLocaleLowerCase()),
  ),
);
let epoch = 0;
async function load() {
  const request = ++epoch;
  busy.value = true;
  failed.value = false;
  packagesFailed.value = false;
  const [catalog, templates, permission] = await Promise.allSettled([
    api.browse(),
    api.packages(),
    api.canManage(),
  ]);
  if (request !== epoch) return;
  if (catalog.status === "fulfilled") items.value = catalog.value;
  else failed.value = true;
  if (templates.status === "fulfilled") packages.value = templates.value;
  else packagesFailed.value = true;
  canCreate.value = permission.status === "fulfilled" && permission.value;
  busy.value = false;
}
function clear() {
  epoch++;
  api.cancel();
  items.value = [];
  packages.value = [];
  canCreate.value = false;
  busy.value = false;
}
onActivated(() => {
  void load();
});
onDeactivated(clear);
watch([() => session.value?.tenant, () => session.value?.subject], () => {
  clear();
  void load();
});
onMounted(() => window.addEventListener("pengolo-access-invalidated", clear));
onBeforeUnmount(() => {
  clear();
  window.removeEventListener("pengolo-access-invalidated", clear);
});
</script>
<template>
  <section class="flex h-full min-h-0 flex-col overflow-hidden bg-canvas">
    <header
      class="shrink-0 border-b border-border bg-surface px-5 py-4 sm:px-8"
    >
      <UiPageHeader
        :title="$t('ontologyManager.allOntologies')"
        :description="$t('ontologyManager.browseDescription')"
        ><template #actions
          ><NuxtLink v-if="canCreate" to="/ontology/new"
            ><UiButton>{{
              $t("ontologyManager.new_ontology")
            }}</UiButton></NuxtLink
          ></template
        ></UiPageHeader
      >
    </header>
    <div class="min-h-0 flex-1 overflow-auto">
      <div class="mx-auto max-w-6xl space-y-6 p-5 sm:p-8">
        <p v-if="busy" role="status" class="text-sm text-muted">
          {{ $t("ontologyManager.loading") }}
        </p>
        <UiAlert v-if="failed" tone="danger"
          >{{ $t("ontologyManager.catalogUnavailable")
          }}<UiButton variant="ghost" @click="load">{{
            $t("pipeline.library.retry")
          }}</UiButton></UiAlert
        >
        <UiInput
          v-model="search"
          :label="$t('ontologyManager.searchOntologies')"
        />
        <h2 class="font-semibold">
          {{ $t("ontologyManager.organization_ontologies") }}
        </h2>
        <ul
          v-if="filtered.length"
          class="divide-y divide-border rounded-panel border border-border bg-surface"
        >
          <li
            v-for="item in filtered"
            :key="item.ontologyId"
            class="flex flex-wrap items-center justify-between gap-3 p-4"
          >
            <div>
              <NuxtLink
                :to="`/ontology/edit/${encodeURIComponent(item.ontologyId)}`"
                class="font-medium text-primary-foreground hover:underline"
                >{{ item.displayName }}</NuxtLink
              >
              <p class="mt-1 text-xs text-muted">{{ item.ontologyId }}</p>
            </div>
            <UiBadge
              :tone="item.productionPublication ? 'success' : 'neutral'"
              >{{
                $t(
                  item.productionPublication
                    ? "pipeline.lifecycle.states.published"
                    : "pipeline.lifecycle.states.draft",
                )
              }}</UiBadge
            >
          </li>
        </ul>
        <UiEmptyState
          v-else-if="!busy && !failed"
          :title="$t('ontologyManager.empty')"
          :description="$t('ontologyManager.browseDescription')"
        />
        <section class="space-y-3">
          <h2 class="font-semibold">
            {{ $t("ontologyManager.plugin_originals") }}
          </h2>
          <p class="text-sm text-muted">
            {{ $t("ontologyManager.pluginHelp") }}
          </p>
          <UiAlert v-if="packagesFailed" tone="warning"
            >{{ $t("ontologyManager.templatesUnavailable")
            }}<UiButton variant="ghost" @click="load">{{
              $t("pipeline.library.retry")
            }}</UiButton></UiAlert
          >
          <ul class="grid gap-3 sm:grid-cols-2">
            <li
              v-for="item in packages"
              :key="item.id"
              class="rounded-panel border border-border bg-surface p-4"
            >
              <NuxtLink
                :to="{ path: '/ontology/new', query: { package: item.id } }"
                class="font-medium text-primary-foreground hover:underline"
                >{{ packageLabel(item.id, item.name) }}</NuxtLink
              >
              <p class="mt-1 text-xs text-muted">
                {{ item.plugin_id }} · {{ item.version }}
              </p>
            </li>
          </ul>
        </section>
      </div>
    </div>
  </section>
</template>
