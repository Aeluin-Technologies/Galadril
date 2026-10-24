<script setup lang="ts">
import { branchLabels } from "~/utils/pipeline";
import type { Json, NodeConfig } from "~/utils/pipeline";
const config = defineModel<NodeConfig>({ required: true });
const { t } = useI18n();
const catalog = usePipelineCatalog();
const models = ref<Array<{ alias: string; model: string }>>([]);
const failed = ref(false),
  loading = ref(false);
let generation = 0;
function param(key: string, value: Json) {
  config.value = {
    ...config.value,
    params: { ...config.value.params, [key]: value },
  };
}
function labels(value: string | undefined) {
  const result = (value ?? "")
    .split(",")
    .map((label) => label.trim())
    .filter(Boolean);
  config.value = {
    ...config.value,
    branch_labels: result,
    params: {
      ...config.value.params,
      match_branch: result[0] ?? "",
      default_branch: result[1] ?? result[0] ?? "",
    },
  };
}
const rows = ref<Array<{ id: string; key: string; value: string }>>([]);
function syncConditions() {
  param(
    "conditions",
    Object.fromEntries(
      rows.value.filter((row) => row.key).map((row) => [row.key, row.value]),
    ),
  );
}
function addCondition() {
  rows.value.push({
    id: crypto.randomUUID(),
    key: "record.metadata.title",
    value: "",
  });
  syncConditions();
}
const fields = computed(() => {
  const value = config.value.params?.metadata_fields;
  return Array.isArray(value)
    ? value
        .filter((item): item is string => typeof item === "string")
        .join(", ")
    : "";
});
const fallback = ref(
  config.value.params?.fallback_models === undefined
    ? "automatic"
    : Array.isArray(config.value.params.fallback_models) &&
        !config.value.params.fallback_models.length
      ? "disabled"
      : "selected",
);
function fallbackPolicy(value: string | undefined) {
  fallback.value = value ?? "automatic";
  const params = { ...config.value.params };
  if (fallback.value === "automatic") delete params.fallback_models;
  else params.fallback_models = [];
  config.value = { ...config.value, params };
}
async function loadModels() {
  const request = ++generation;
  loading.value = true;
  failed.value = false;
  try {
    const values = await catalog.models();
    if (request === generation) models.value = values;
  } catch {
    if (request === generation) failed.value = true;
  } finally {
    if (request === generation) loading.value = false;
  }
}
function strategy(value: string | undefined) {
  if (value !== "regex" && value !== "slm" && value !== "metadata") return;
  const common = {
    field: "record.raw_payload.content",
    match_branch: branchLabels(config.value)[0] ?? "match",
    default_branch: branchLabels(config.value)[1] ?? "other",
  };
  const params: Record<string, Json> =
    value === "regex"
      ? { ...common, pattern: "appeal" }
      : value === "slm"
        ? {
            field: common.field,
            instructions: t("pipeline.composition.default_instructions"),
          }
        : {
            conditions: { "record.metadata.title": "" },
            match_branch: common.match_branch,
            default_branch: common.default_branch,
          };
  config.value = { ...config.value, routing: value, model: undefined, params };
  fallback.value = "automatic";
}
watch(
  () => config.value.routing,
  (value) => {
    if (value === "slm") void loadModels();
    if (value === "metadata") {
      const values = config.value.params?.conditions;
      rows.value =
        values && typeof values === "object" && !Array.isArray(values)
          ? Object.entries(values).map(([key, value]) => ({
              id: crypto.randomUUID(),
              key,
              value: String(value ?? ""),
            }))
          : [];
    }
  },
  { immediate: true },
);
onDeactivated(() => {
  generation++;
});
const choices = computed(() => [
  { value: "", label: t("pipeline.composition.default_model") },
  ...models.value.map((model) => ({
    value: model.alias,
    label: `${model.alias} · ${model.model}`,
  })),
  ...(config.value.model &&
  !models.value.some((model) => model.alias === config.value.model)
    ? [
        {
          value: config.value.model,
          label: t("pipeline.composition.missing_model", {
            model: config.value.model,
          }),
        },
      ]
    : []),
]);
</script>
<template>
  <div class="grid gap-4">
    <UiSelect
      :model-value="config.routing ?? 'regex'"
      :label="$t('pipeline.composition.method')"
      :options="[
        { value: 'regex', label: $t('pipeline.composition.regex') },
        { value: 'slm', label: $t('pipeline.composition.slm') },
        { value: 'metadata', label: $t('pipeline.composition.metadata') },
      ]"
      @update:model-value="strategy"
    />
    <UiInput
      :model-value="branchLabels(config).join(', ')"
      :label="$t('pipeline.composition.branch_labels')"
      :help="$t('pipeline.composition.branch_help')"
      @update:model-value="labels"
    />
    <UiInput
      v-if="config.routing !== 'metadata'"
      :model-value="
        String(config.params?.field ?? 'record.raw_payload.content')
      "
      :label="$t('pipeline.composition.content_field')"
      @update:model-value="param('field', $event ?? '')"
    />
    <UiInput
      v-if="config.routing === 'regex'"
      :model-value="String(config.params?.pattern ?? '')"
      :label="$t('pipeline.composition.pattern')"
      @update:model-value="param('pattern', $event ?? '')"
    />
    <template v-if="config.routing === 'metadata'">
      <div
        v-for="row in rows"
        :key="row.id"
        class="grid gap-3 rounded border border-border p-3"
      >
        <UiInput
          v-model="row.key"
          :label="$t('pipeline.composition.metadata_key')"
          @update:model-value="syncConditions"
        />
        <UiInput
          v-model="row.value"
          :label="$t('pipeline.composition.expected_value')"
          @update:model-value="syncConditions"
        />
        <UiButton
          variant="ghost"
          @click="
            rows = rows.filter((item) => item.id !== row.id);
            syncConditions();
          "
          >{{ $t("ui.actions.delete") }}</UiButton
        >
      </div>
      <UiButton
        variant="secondary"
        :disabled="rows.length >= 16"
        @click="addCondition"
        >{{ $t("pipeline.composition.add_condition") }}</UiButton
      >
    </template>
    <template v-if="config.routing === 'slm'">
      <UiAlert v-if="failed" tone="danger">{{
        $t("pipeline.composition.models_unavailable")
      }}</UiAlert>
      <UiSelect
        :model-value="config.model ?? ''"
        :label="$t('pipeline.composition.model')"
        :options="choices"
        :disabled="loading"
        @update:model-value="config = { ...config, model: $event || undefined }"
      />
      <UiButton v-if="failed" variant="secondary" :action="loadModels">{{
        $t("pipeline.composition.refresh")
      }}</UiButton>
      <UiTextarea
        :model-value="String(config.params?.instructions ?? '')"
        :label="$t('pipeline.composition.instructions')"
        @update:model-value="param('instructions', $event ?? '')"
      />
      <UiInput
        :model-value="fields"
        :label="$t('pipeline.composition.metadata_fields')"
        @update:model-value="
          param(
            'metadata_fields',
            ($event ?? '')
              .split(',')
              .map((value) => value.trim())
              .filter(Boolean),
          )
        "
      />
      <UiSelect
        :model-value="fallback"
        :label="$t('pipeline.composition.fallback')"
        :options="[
          { value: 'automatic', label: $t('pipeline.composition.automatic') },
          {
            value: 'selected',
            label: $t('pipeline.composition.selected_models'),
          },
          { value: 'disabled', label: $t('pipeline.composition.no_fallback') },
        ]"
        @update:model-value="fallbackPolicy"
      />
      <UiInput
        v-if="fallback === 'selected'"
        :model-value="
          Array.isArray(config.params?.fallback_models)
            ? config.params.fallback_models.join(', ')
            : ''
        "
        :label="$t('pipeline.composition.fallback_models')"
        @update:model-value="
          param(
            'fallback_models',
            ($event ?? '')
              .split(',')
              .map((value) => value.trim())
              .filter(Boolean),
          )
        "
      />
    </template>
  </div>
</template>
