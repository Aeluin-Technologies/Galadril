<script setup lang="ts">
const props = defineProps<{ pipelineId: string; revision: string }>();
const emit = defineEmits<{ select: [revision: string] }>();
const catalog = usePipelineCatalog();
type History = Awaited<ReturnType<typeof catalog.history>>;
const items = ref<History["items"]>([]);
const after = ref<string | null>(null);
const loading = ref(false);
const failed = ref(false);
let epoch = 0;
async function load(more = false) {
  if (loading.value) return;
  const request = ++epoch;
  loading.value = true;
  failed.value = false;
  try {
    const result = await catalog.history(
      props.pipelineId,
      more ? (after.value ?? undefined) : undefined,
    );
    if (request === epoch) {
      items.value = more ? [...items.value, ...result.items] : result.items;
      after.value = result.after;
    }
  } catch {
    if (request === epoch) failed.value = true;
  } finally {
    if (request === epoch) loading.value = false;
  }
}
watch(
  () => props.revision,
  () => {
    epoch++;
    catalog.cancel();
    loading.value = false;
    items.value = [];
    void load();
  },
  { immediate: true },
);
onBeforeUnmount(() => {
  epoch++;
});
</script>
<template>
  <div class="space-y-3">
    <p v-if="loading" role="status">
      {{ $t("pipeline.library.loadingHistory") }}
    </p>
    <UiAlert v-if="failed" tone="danger"
      >{{ $t("pipeline.library.historyError")
      }}<UiButton variant="ghost" @click="load()">{{
        $t("pipeline.library.retry")
      }}</UiButton></UiAlert
    >
    <ul class="max-h-64 divide-y divide-border overflow-y-auto">
      <li
        v-for="item in items"
        :key="item.revision_id"
        class="flex flex-wrap items-center justify-between gap-3 py-3"
      >
        <div class="min-w-0">
          <p class="text-foreground">{{ item.message || item.name }}</p>
          <p class="break-all text-xs">
            {{ item.author_id }} ·
            {{ new Date(item.updated_at_ms).toLocaleString() }} ·
            {{ item.revision_id.slice(0, 12) }}
          </p>
        </div>
        <UiButton
          variant="secondary"
          size="sm"
          @click="emit('select', item.revision_id)"
          >{{ $t("pipeline.library.loadVersion") }}</UiButton
        >
      </li>
    </ul>
    <UiButton
      v-if="after"
      variant="ghost"
      :loading="loading"
      @click="load(true)"
      >{{ $t("pipeline.library.moreVersions") }}</UiButton
    >
  </div>
</template>
