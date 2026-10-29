<script setup lang="ts">
import type { NodeConfig, StepKind } from "~/utils/pipeline";
import { pluginConfig } from "~/utils/plugin-blocks";
import type { PluginBlock } from "~/utils/plugin-blocks";
const props = defineProps<{ kind: StepKind }>();
const config = defineModel<NodeConfig>({ required: true });
const catalog = usePipelineCatalog();
const entries = shallowRef<PluginBlock[]>([]);
const loading = ref(false),
  failed = ref(false);
const selected = ref("");
const current = computed(() =>
  entries.value.find((item) => item.id === selected.value),
);
let epoch = 0;
async function load() {
  const own = ++epoch;
  loading.value = true;
  failed.value = false;
  try {
    const result = await catalog.capabilities();
    if (own !== epoch) return;
    entries.value = result.filter((item) =>
      props.kind === "inference"
        ? item.kind === "MODEL" && item.execution === "REGISTERED"
        : item.pluginId === "core" && item.stepType === props.kind,
    );
    selected.value =
      entries.value.find(
        (item) =>
          item.modelClass === config.value.model ||
          item.compatibility.allowedModels.includes(config.value.model ?? ""),
      )?.id ??
      entries.value[0]?.id ??
      "";
  } catch {
    if (own === epoch) failed.value = true;
  } finally {
    if (own === epoch) loading.value = false;
  }
}
watch(selected, () => {
  const item = current.value;
  if (props.kind !== "inference" || !item) return;
  if (item.pluginId !== "core")
    config.value = { ...config.value, ...pluginConfig(item) };
  else {
    const {
      capability_id: _id,
      capability_version: _version,
      ...settings
    } = config.value;
    config.value = {
      ...settings,
      model: item.modelClass ?? item.compatibility.allowedModels[0],
    };
  }
});
onMounted(load);
onBeforeUnmount(() => {
  epoch++;
  catalog.cancel();
});
</script>
<template>
  <div class="space-y-3">
    <p v-if="loading" role="status">{{ $t("pipeline.composition.loading") }}</p>
    <UiAlert v-if="failed" tone="danger"
      >{{ $t("pipeline.plugins.unavailable")
      }}<UiButton variant="ghost" :action="load">{{
        $t("pipeline.library.retry")
      }}</UiButton></UiAlert
    >
    <UiSelect
      v-if="entries.length"
      v-model="selected"
      :label="
        $t(
          kind === 'inference'
            ? 'pipeline.config.inference.model'
            : 'pipeline.inspector.operation',
        )
      "
      :options="entries.map((item) => ({ value: item.id, label: item.name }))"
    />
    <PipelinePluginParameters
      v-if="current"
      :key="current.id"
      v-model="config"
      :capability="current"
    />
    <details v-if="kind === 'source'">
      <summary class="cursor-pointer text-sm">
        {{ $t("pipeline.plugins.advanced") }}
      </summary>
      <div class="mt-3 space-y-3">
        <UiInput
          v-model="config.topic"
          :label="$t('pipeline.config.source.topic')"
        /><UiInput
          v-model="config.match_pattern"
          :label="$t('pipeline.config.source.regex')"
        /><UiInput
          v-model="config.schema_path"
          :label="$t('pipeline.inspector.schema')"
        />
      </div>
    </details>
    <details v-if="kind === 'inference'">
      <summary class="cursor-pointer text-sm">
        {{ $t("pipeline.plugins.advanced") }}
      </summary>
      <UiInput
        v-model="config.model"
        :label="$t('pipeline.inspector.modelPath')"
        class="mt-3"
      />
    </details>
  </div>
</template>
