<script setup lang="ts">
import type { PipelineSummary } from "~/utils/pipeline";
const catalog = usePipelineCatalog();
const items = shallowRef<PipelineSummary[]>([]);
const loading = ref(false);
const failed = ref(false);
let epoch = 0;
async function refresh() {
  const request = ++epoch;
  loading.value = true;
  failed.value = false;
  try {
    const result = await catalog.browse();
    if (request === epoch) items.value = result;
  } catch {
    if (request === epoch) failed.value = true;
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
      >{{ $t("pipeline.library.loadError")
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
    <ul
      v-if="items.length"
      class="divide-y divide-border rounded-panel border border-border bg-surface"
    >
      <li
        v-for="item in items"
        :key="item.pipeline_id"
        class="flex flex-wrap items-center justify-between gap-3 p-5"
      >
        <div class="min-w-0">
          <NuxtLink
            :to="`/pipeline/${encodeURIComponent(item.pipeline_id)}`"
            class="font-medium text-info"
            >{{ item.name }}</NuxtLink
          >
          <p class="mt-1 truncate text-xs text-muted">{{ item.pipeline_id }}</p>
        </div>
        <UiBadge
          :tone="
            item.published_revision_id === item.head_revision_id
              ? 'success'
              : 'neutral'
          "
          >{{
            item.published_revision_id === item.head_revision_id
              ? $t("pipeline.library.published")
              : $t("pipeline.library.draft")
          }}</UiBadge
        >
      </li>
    </ul>
  </section>
</template>
