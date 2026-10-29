<script setup lang="ts">
import { pluginBlocks } from "~/utils/plugin-blocks";
import type { PluginBlock } from "~/utils/plugin-blocks";
import type { NodeConfig } from "~/utils/pipeline";
const config = defineModel<NodeConfig>({ required: true });
const emit = defineEmits<{
  valid: [value: boolean];
  available: [value: boolean];
}>();
const catalog = usePipelineCatalog();
const capability = shallowRef<PluginBlock>();
const failed = ref(false),
  loading = ref(false);
let generation = 0;
async function load() {
  const request = ++generation;
  loading.value = true;
  failed.value = false;
  emit("valid", false);
  emit("available", false);
  try {
    const entries = pluginBlocks(await catalog.capabilities());
    if (request !== generation) return;
    const item = entries.find(
      (item) =>
        item.id === config.value.capability_id &&
        (!config.value.capability_version ||
          item.pluginVersion === config.value.capability_version),
    );
    if (!item) throw new Error("PLUGIN_UNAVAILABLE");
    capability.value = item;
    emit("available", true);
  } catch {
    if (request === generation) {
      capability.value = undefined;
      failed.value = true;
    }
  } finally {
    if (request === generation) loading.value = false;
  }
}
function cancel() {
  generation++;
  catalog.cancel();
  emit("valid", false);
  emit("available", false);
}
onMounted(load);
onBeforeUnmount(cancel);
onDeactivated(cancel);
onActivated(() => {
  if (generation) void load();
});
</script>
<template>
  <div class="grid gap-3">
    <details class="text-xs text-muted">
      <summary class="cursor-pointer">
        {{ $t("pipeline.inspector.identity") }}
      </summary>
      <p class="mt-2 break-all">
        {{ config.capability_id }} · {{ config.capability_version }}
      </p>
    </details>
    <p v-if="loading" role="status">{{ $t("pipeline.composition.loading") }}</p>
    <UiAlert v-if="failed" tone="danger">{{
      $t("pipeline.plugins.unavailable")
    }}</UiAlert>
    <UiSelect
      v-if="capability?.compatibility.allowedModels.length"
      v-model="config.model"
      :label="$t('pipeline.config.inference.model')"
      :options="
        capability.compatibility.allowedModels.map((value) => ({
          value,
          label: value,
        }))
      "
    />
    <PipelinePluginParameters
      v-if="capability && !loading"
      :key="capability.id + capability.pluginVersion"
      v-model="config"
      :capability="capability"
      @valid="emit('valid', $event)"
    />
    <UiButton v-if="failed" :action="load" variant="secondary">{{
      $t("pipeline.composition.refresh")
    }}</UiButton>
  </div>
</template>
