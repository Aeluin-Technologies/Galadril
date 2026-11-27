<script setup lang="ts">
import type { GolOntologyPackage, GolSummary } from "~/types/ontology";
const api = useOntologyCatalog(),
  { session } = useAccount();
const items = shallowRef<GolSummary[]>([]),
  packages = shallowRef<GolOntologyPackage[]>([]),
  search = ref("");
const busy = ref(false),
  failed = ref(false),
  packagesFailed = ref(false),
  installFailed = ref(false),
  canCreate = ref(false);
let epoch = 0;
const filtered = computed(() =>
  items.value.filter((item) =>
    `${item.displayName} ${item.ontologyId}`
      .toLocaleLowerCase()
      .includes(search.value.trim().toLocaleLowerCase()),
  ),
);
const available = computed(() => {
  const installed = new Map(items.value.map((item) => [item.ontologyId, item]));
  const term = search.value.trim().toLocaleLowerCase();
  return packages.value.flatMap<
    GolOntologyPackage & { expectedRevision?: string }
  >((item) => {
    if (
      !`${item.displayName} ${item.ontologyId} ${item.pluginId}`
        .toLocaleLowerCase()
        .includes(term)
    )
      return [];
    const current = installed.get(item.ontologyId);
    if (!current) return [{ ...item, expectedRevision: undefined }];
    if (
      current.origin?.pluginId === item.pluginId &&
      (current.origin.pluginVersion !== item.pluginVersion ||
        current.origin.packageVersion !== item.packageVersion)
    )
      return [{ ...item, expectedRevision: current.sourceRevision }];
    return [];
  });
});
function clear() {
  epoch++;
  api.cancel();
  items.value = [];
  packages.value = [];
  packagesFailed.value = false;
  installFailed.value = false;
  canCreate.value = false;
  busy.value = false;
}
async function load() {
  const request = ++epoch;
  busy.value = true;
  failed.value = false;
  packagesFailed.value = false;
  try {
    const [installed, allowed, inventory] = await Promise.all([
      api.browse().catch(() => null),
      api.canManage().catch(() => false),
      api.packages().catch(() => null),
    ]);
    if (request !== epoch) return;
    if (installed) items.value = installed;
    failed.value = installed === null;
    packages.value = inventory ?? [];
    packagesFailed.value = allowed && inventory === null;
    canCreate.value = allowed;
  } finally {
    if (request === epoch) busy.value = false;
  }
}
async function install(item: GolOntologyPackage, expectedRevision?: string) {
  const request = epoch;
  installFailed.value = false;
  try {
    const installed = await api.install(item, expectedRevision);
    if (request !== epoch) return;
    items.value = [
      ...items.value.filter(
        (value) => value.ontologyId !== installed.ontologyId,
      ),
      installed,
    ];
    await load();
  } catch {
    if (request === epoch) installFailed.value = true;
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
              <p v-if="item.origin" class="mt-1 text-xs text-muted">
                {{
                  $t("gol.packageOrigin", {
                    plugin: item.origin.pluginId,
                    version: item.origin.packageVersion,
                  })
                }}
              </p>
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
        <UiAlert v-if="packagesFailed" tone="danger">
          {{ $t("gol.packagesUnavailable") }}
          <UiButton variant="ghost" :action="load">{{
            $t("gol.retry")
          }}</UiButton>
        </UiAlert>
        <UiAlert v-if="installFailed" tone="danger">
          {{ $t("gol.packageInstallFailed") }}
        </UiAlert>
        <section
          v-if="available.length && !failed"
          aria-labelledby="gol-packages-heading"
          class="space-y-3"
        >
          <h2 id="gol-packages-heading" class="text-base font-semibold">
            {{ $t("gol.availablePackages") }}
          </h2>
          <ul
            class="divide-y divide-border rounded-panel border border-border bg-surface"
          >
            <li
              v-for="item in available"
              :key="item.ontologyId"
              class="flex flex-wrap items-center justify-between gap-4 p-4"
            >
              <div>
                <p class="font-medium">{{ item.displayName }}</p>
                <p class="mt-1 text-xs text-muted">
                  {{
                    $t("gol.packageOrigin", {
                      plugin: item.pluginId,
                      version: item.packageVersion,
                    })
                  }}
                </p>
                <p
                  v-if="item.requiredPlugins.length"
                  class="mt-1 text-xs text-muted"
                >
                  {{
                    $t("gol.packageRequires", {
                      plugins: item.requiredPlugins
                        .map(
                          ({ pluginId, pluginVersion }) =>
                            `${pluginId} ${pluginVersion}`,
                        )
                        .join(", "),
                    })
                  }}
                </p>
              </div>
              <UiButton
                variant="secondary"
                :aria-label="
                  $t(
                    item.expectedRevision
                      ? 'gol.updateNamedPackage'
                      : 'gol.installNamedPackage',
                    { name: item.displayName },
                  )
                "
                :action="() => install(item, item.expectedRevision)"
              >
                {{
                  $t(
                    item.expectedRevision
                      ? "gol.updateDraft"
                      : "gol.installDraft",
                  )
                }}
              </UiButton>
            </li>
          </ul>
        </section>
      </div>
    </div>
  </section>
</template>
