<script setup lang="ts">
import { useOnboardingStore } from "~/stores/useOnboarding";
import type { PipelineSummary } from "~/utils/pipeline";
const catalog = usePipelineCatalog();
const items = shallowRef<PipelineSummary[]>([]);
const loading = ref(false);
const failed = ref(false);
const denied = ref(false);
const search = ref(""),
  state = ref("all");
const { t } = useI18n();
const filtered = computed(() =>
  items.value.filter(
    (item) =>
      (state.value === "all" || (item.lifecycle ?? "draft") === state.value) &&
      `${item.name} ${item.pipeline_id} ${(item.branch_names ?? []).join(" ")}`
        .toLocaleLowerCase()
        .includes(search.value.trim().toLocaleLowerCase()),
  ),
);
let epoch = 0;
async function refresh() {
  const request = ++epoch;
  loading.value = true;
  failed.value = false;
  denied.value = false;
  try {
    const result = await catalog.browse();
    if (result.length) useOnboardingStore().setStepCompleted("pipeline");
    if (request === epoch) items.value = result;
  } catch (error) {
    if (request === epoch) {
      failed.value = true;
      denied.value = error instanceof Error && error.message === "FORBIDDEN";
    }
  } finally {
    if (request === epoch) loading.value = false;
  }
}
onActivated(() => {
  void refresh();
});
onDeactivated(() => {
  epoch++;
  catalog.cancel();
  items.value = [];
});
</script>
<template>
  <section class="mx-auto w-full max-w-6xl space-y-6 p-5 sm:p-8">
    <UiPageHeader
      :title="$t('pipeline.library.title')"
      :description="$t('pipeline.library.description')"
      ><template #actions
        ><NuxtLink to="/pipeline/new"
          ><UiButton>{{ $t("pipeline.library.create") }}</UiButton></NuxtLink
        ></template
      ></UiPageHeader
    >
    <UiAlert v-if="failed" tone="danger"
      >{{
        $t(
          denied ? "pipeline.library.forbidden" : "pipeline.library.loadError",
        )
      }}<UiButton variant="ghost" :disabled="loading" @click="refresh">{{
        $t("pipeline.library.retry")
      }}</UiButton></UiAlert
    >
    <p v-if="loading" role="status" class="text-muted">
      {{ $t("pipeline.library.loading") }}
    </p>
    <p
      v-else-if="!items.length && !failed"
      class="rounded-panel border border-border bg-surface p-6 text-muted"
    >
      {{ $t("pipeline.library.empty") }}
    </p>
    <div class="flex flex-wrap items-end gap-3">
      <UiInput
        v-model="search"
        :label="$t('pipeline.library.search')"
        class="min-w-0 flex-1"
      /><UiSelect
        v-model="state"
        :label="$t('pipeline.library.status')"
        :options="[
          { value: 'all', label: t('pipeline.library.allStates') },
          ...['draft', 'approved', 'published', 'deployed'].map((value) => ({
            value,
            label: t(`pipeline.lifecycle.states.${value}`),
          })),
        ]"
      />
    </div>
    <p v-if="items.length && !filtered.length" class="text-sm text-muted">
      {{ $t("pipeline.library.filteredEmpty") }}
    </p>
    <div
      v-if="filtered.length"
      role="region"
      :aria-label="$t('pipeline.library.title')"
      tabindex="0"
      class="overflow-x-auto rounded-panel border border-border bg-surface"
    >
      <table class="w-full text-start text-sm">
        <thead class="bg-surface-muted text-muted">
          <tr>
            <th scope="col" class="p-4 text-start font-medium">
              {{ $t("pipeline.header.pipeline_name") }}
            </th>
            <th scope="col" class="p-4 text-start font-medium">
              {{ $t("pipeline.library.branches") }}
            </th>
            <th scope="col" class="p-4 text-start font-medium">
              {{ $t("pipeline.library.revision") }}
            </th>
            <th scope="col" class="p-4 text-start font-medium">
              {{ $t("pipeline.library.status") }}
            </th>
          </tr>
        </thead>
        <tbody class="divide-y divide-border">
          <tr v-for="item in filtered" :key="item.pipeline_id">
            <th scope="row" class="p-4 text-start font-medium">
              <NuxtLink
                :to="`/pipeline/${encodeURIComponent(item.pipeline_id)}`"
                class="text-info"
                >{{ item.name }}</NuxtLink
              >
              <p class="mt-1 text-xs font-normal text-muted">
                {{ item.pipeline_id }}
              </p>
            </th>
            <td class="p-4">
              <div class="flex flex-wrap gap-1">
                <NuxtLink
                  v-for="name in item.branch_names ?? ['main']"
                  :key="name"
                  :to="{
                    path: `/pipeline/${encodeURIComponent(item.pipeline_id)}`,
                    query: { branch: name },
                  }"
                  class="inline-flex items-center gap-1 rounded-control border border-border bg-surface-muted px-2 py-1 text-xs text-info hover:bg-info-subtle"
                  ><span aria-hidden="true">⑂</span>{{ name }}</NuxtLink
                >
              </div>
            </td>
            <td class="p-4 font-mono text-xs text-muted">
              {{ item.head_revision_id.slice(0, 12) }}
            </td>
            <td class="p-4">
              <UiBadge
                :tone="
                  item.lifecycle === 'deployed'
                    ? 'success'
                    : item.lifecycle === 'approved'
                      ? 'info'
                      : 'neutral'
                "
                >{{
                  $t(`pipeline.lifecycle.states.${item.lifecycle ?? "draft"}`)
                }}</UiBadge
              >
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
