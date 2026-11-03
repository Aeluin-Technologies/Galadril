<script setup lang="ts">
import {
  parameterFields,
  parseParameters,
  sourceConfig,
  sourceParameters,
} from "~/utils/plugin-blocks";
import type { PluginBlock } from "~/utils/plugin-blocks";
import type { Json, NodeConfig } from "~/utils/pipeline";
const { t, te } = useI18n();
const props = defineProps<{ capability: PluginBlock }>();
const config = defineModel<NodeConfig>({ required: true });
const emit = defineEmits<{ valid: [value: boolean] }>();
const fields = computed(() => parameterFields(props.capability));
const values = computed(() =>
  props.capability.stepType === "source"
    ? sourceParameters(config.value)
    : (config.value.params ?? {}),
);
const text = ref(JSON.stringify(values.value, null, 2));
const failed = ref(false);
const advanced = ref(false);
const models = ref<Array<{ value: string; label: string }>>([]);
const modelFailed = ref(false);
const catalog = usePipelineCatalog();
let generation = 0;
async function loadModels() {
  const own = ++generation;
  try {
    const result = await catalog.models();
    if (own === generation)
      models.value = result.map((item) => ({
        value: item.alias,
        label: item.alias,
      }));
  } catch {
    if (own === generation) modelFailed.value = true;
  }
}
onMounted(() => {
  if (fields.value.some((field) => field.format === "model-alias"))
    void loadModels();
});
onBeforeUnmount(() => {
  generation++;
  catalog.cancel();
});
const schemas = [
  "audio",
  "document",
  "email",
  "image",
  "observation_context",
  "sensor",
  "text",
  "transaction",
  "video",
];
const schemaOptions = computed(() => {
  const options = schemas.map((name) => ({
    value: `schemas/avro/${name}.avsc`,
    label: t(`pipeline.schemas.${name}`),
  }));
  const stored = values.value.schema_path;
  if (typeof stored !== "string" || !stored) return options;
  const name = schemas.find((item) => stored === `${item}.avsc`);
  if (name)
    return options.map((item) =>
      item.value.endsWith(`/${name}.avsc`) ? { ...item, value: stored } : item,
    );
  return options.some((item) => item.value === stored)
    ? options
    : [
        ...options,
        { value: stored, label: t("pipeline.schemas.custom"), disabled: true },
      ];
});
function label(field: (typeof fields.value)[number]): string {
  const key = `pipeline.review.fields.${field.key}`;
  if (
    field.label !== field.key &&
    !["topic", "match_pattern", "schema_path", "action"].includes(field.key)
  )
    return field.label;
  if (te(key)) return t(key);
  return field.label === field.key
    ? field.key
        .replaceAll("_", " ")
        .replace(/^./, (first) => first.toUpperCase())
    : field.label;
}
function help(field: (typeof fields.value)[number]): string | undefined {
  const key = `pipeline.fieldHelp.${field.key}`;
  return te(key) ? t(key) : field.help;
}
const visibleFields = computed(() =>
  fields.value.filter(
    (field) =>
      (advanced.value ||
        (!field.advanced &&
          !(props.capability.kind === "MODEL" && field.key === "action"))) &&
      (field.key !== "cron" || values.value.trigger === "cron") &&
      (props.capability.id !== "utils.algorithm.compose_reply" ||
        ((field.key !== "template" || values.value.generation === "template") &&
          (!["model_alias", "instructions"].includes(field.key) ||
            values.value.generation === "scribe"))),
  ),
);
function publish(value: Record<string, Json>) {
  config.value =
    props.capability.stepType === "source"
      ? sourceConfig(config.value, value)
      : { ...config.value, params: value };
  const complete = fields.value.every(
    (field) =>
      !field.required ||
      (value[field.key] !== undefined && value[field.key] !== ""),
  );
  emit("valid", !failed.value && complete);
}
function update(
  key: string,
  type: string,
  value: string | number | boolean | undefined,
) {
  const field = fields.value.find((field) => field.key === key);
  if (value === "" && !field?.required) {
    const next = { ...values.value };
    delete next[key];
    failed.value = false;
    publish(next);
    text.value = JSON.stringify(next, null, 2);
    return;
  }
  const parsed =
    type === "integer" || type === "number" ? Number(value) : (value ?? "");
  if (
    (type === "integer" || type === "number") &&
    (value === "" ||
      !Number.isFinite(parsed) ||
      (type === "integer" && !Number.isInteger(parsed)))
  ) {
    failed.value = true;
    emit("valid", false);
    return;
  }
  failed.value = false;
  const next = { ...values.value, [key]: parsed };
  publish(next);
  text.value = JSON.stringify(next, null, 2);
}
function edit(value: string | undefined) {
  text.value = value ?? "";
  try {
    const next = parseParameters(text.value);
    failed.value = false;
    publish(next);
  } catch {
    failed.value = true;
    emit("valid", false);
  }
}
onMounted(() => publish(values.value));
</script>
<template>
  <div class="grid gap-3">
    <p class="text-sm text-muted">{{ capability.description }}</p>
    <template v-for="field in visibleFields" :key="field.key">
      <UiSelect
        v-if="field.key === 'schema_path' && capability.stepType === 'source'"
        :label="label(field)"
        :help="help(field)"
        :model-value="String(values[field.key] ?? '')"
        :options="schemaOptions"
        @update:model-value="update(field.key, field.type, $event)"
      />
      <UiCheckbox
        v-else-if="field.type === 'boolean'"
        :label="label(field)"
        :model-value="values[field.key] === true"
        @update:model-value="update(field.key, field.type, $event)"
      />
      <UiSelect
        v-else-if="field.options"
        :label="label(field)"
        :model-value="String(values[field.key] ?? '')"
        :options="field.options.map((value) => ({ value, label: value }))"
        @update:model-value="update(field.key, field.type, $event)"
      />
      <UiSelect
        v-else-if="field.format === 'model-alias'"
        :label="label(field)"
        :help="help(field)"
        :model-value="String(values[field.key] ?? '')"
        :options="models"
        @update:model-value="update(field.key, field.type, $event)"
      />
      <PipelineEmailAccountField
        v-else-if="
          field.format === 'email-source-account' ||
          field.format === 'email-sender-account'
        "
        :label="label(field)"
        :help="help(field)"
        :sender="field.format === 'email-sender-account'"
        :model-value="String(values[field.key] ?? '')"
        @update:model-value="update(field.key, field.type, $event)"
      />
      <UiTextarea
        v-else-if="field.format === 'multiline'"
        :label="label(field)"
        :help="help(field)"
        :model-value="String(values[field.key] ?? '')"
        rows="5"
        @update:model-value="update(field.key, field.type, $event)"
      />
      <UiInput
        v-else
        :label="label(field)"
        :required="field.required"
        :help="help(field)"
        :min="field.minimum"
        :max="field.maximum"
        :type="
          field.type === 'integer' || field.type === 'number'
            ? 'number'
            : 'text'
        "
        :model-value="String(values[field.key] ?? '')"
        @update:model-value="update(field.key, field.type, $event)"
      />
    </template>
    <UiButton
      v-if="capability.id !== 'core.input.kafka'"
      variant="ghost"
      @click="advanced = !advanced"
      >{{ $t("pipeline.plugins.advanced") }}</UiButton
    >
    <UiTextarea
      v-if="advanced"
      :label="$t('pipeline.plugins.parameters')"
      :model-value="text"
      rows="8"
      @update:model-value="edit"
    />
    <UiAlert v-if="modelFailed" tone="danger">{{
      $t("pipeline.composition.models_unavailable")
    }}</UiAlert>
    <UiAlert v-if="failed" tone="danger">{{
      $t("pipeline.plugins.invalid")
    }}</UiAlert>
  </div>
</template>
