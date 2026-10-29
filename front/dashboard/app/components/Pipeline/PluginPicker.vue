<script setup lang="ts">
import { pluginBlocks } from "~/utils/plugin-blocks";
import type { PluginBlock } from "~/utils/plugin-blocks";
const open = defineModel<boolean>({ required: true });
const emit = defineEmits<{ select: [capability: PluginBlock] }>();
const catalog = usePipelineCatalog();
const entries = shallowRef<PluginBlock[]>([]);
const selected = ref("");
const plugin = ref("");
const plugins = computed(() => [
  ...new Set(
    entries.value
      .filter((item) => item.kind !== "MODEL")
      .map((item) => item.pluginId),
  ),
]);
const operations = computed(() =>
  entries.value.filter(
    (item) => item.pluginId === plugin.value && item.kind !== "MODEL",
  ),
);
watch(plugin, () => {
  selected.value = "";
});
const loading = ref(false),
  failed = ref(false);
const current = computed(() =>
  entries.value.find((item) => item.id === selected.value),
);
let generation = 0;
async function refresh() {
  const request = ++generation;
  loading.value = true;
  failed.value = false;
  try {
    const result = pluginBlocks(await catalog.capabilities()).filter(
      (item) => item.kind !== "MODEL",
    );
    if (request === generation && open.value) {
      entries.value = result;
      if (!result.some((item) => item.id === selected.value))
        selected.value = "";
    }
  } catch {
    if (request === generation) {
      failed.value = true;
      entries.value = [];
    }
  } finally {
    if (request === generation) loading.value = false;
  }
}
watch(open, (value) => {
  if (value) void refresh();
  else {
    generation++;
    catalog.cancel();
  }
});
onDeactivated(() => {
  generation++;
  catalog.cancel();
});
onActivated(() => {
  if (open.value) void refresh();
});
async function add() {
  const expected = current.value;
  if (!expected) return;
  await refresh();
  const available = current.value;
  if (
    !open.value ||
    !available ||
    available.pluginVersion !== expected.pluginVersion
  ) {
    failed.value = true;
    return;
  }
  emit("select", available);
  open.value = false;
}
</script>
<template>
  <UiDialog v-model:open="open" :title="$t('pipeline.plugins.title')">
    <div class="grid gap-4">
      <p class="text-sm text-muted">{{ $t("pipeline.plugins.description") }}</p>
      <p v-if="loading" role="status">
        {{ $t("pipeline.composition.loading") }}
      </p>
      <UiAlert v-if="failed" tone="danger">{{
        $t("pipeline.plugins.unavailable")
      }}</UiAlert>
      <p v-else-if="!loading && !entries.length" role="status">
        {{ $t("pipeline.plugins.empty") }}
      </p>
      <UiSelect
        v-model="plugin"
        :label="$t('pipeline.plugins.plugin')"
        :disabled="loading"
        :options="plugins.map((value) => ({ value, label: value }))"
      />
      <UiSelect
        v-if="plugin"
        v-model="selected"
        :label="$t('pipeline.plugins.block')"
        :disabled="loading"
        :options="
          operations.map((item) => ({
            value: item.id,
            label: `${item.name} · ${item.pluginId} ${item.pluginVersion}`,
          }))
        "
      />
      <template v-if="current">
        <p>{{ current.description }}</p>
        <p
          v-if="current.compatibility.requires.length"
          class="text-sm text-muted"
        >
          {{ $t("pipeline.plugins.requires") }}:
          {{ current.compatibility.requires.join(", ") }}
        </p>
      </template>
      <div class="flex flex-wrap gap-2">
        <UiButton
          :action="add"
          :disabled="!current || loading"
          @action-error="failed = true"
          >{{ $t("pipeline.composition.add_block") }}</UiButton
        >
        <UiButton variant="secondary" :action="refresh">{{
          $t("pipeline.composition.refresh")
        }}</UiButton>
      </div>
    </div>
  </UiDialog>
</template>
