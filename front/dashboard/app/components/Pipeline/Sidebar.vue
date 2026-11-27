<script setup lang="ts">
import type { GraphNode, NodeConfig } from "~/utils/pipeline";

type Node = GraphNode & {
  data: { config: NodeConfig & { connector?: string } };
};
defineProps<{ hasIncoming: boolean }>();
const activeNode = defineModel<Node | null>("activeNode");
const emit = defineEmits<{ close: [] }>();

function updateConfig(changes: Partial<Node["data"]["config"]>) {
  const node = activeNode.value;
  if (!node) return;

  activeNode.value = {
    ...node,
    data: { ...node.data, config: { ...node.data.config, ...changes } },
  };
}

function option(
  key: "topic" | "match_pattern" | "schema_path" | "model" | "connector",
) {
  return computed({
    get: () => activeNode.value?.data.config[key],
    set: (value: string | undefined) => updateConfig({ [key]: value }),
  });
}

function updateParameter(key: string, value: string | number | undefined) {
  const params = activeNode.value?.data.config.params;
  if (!params) return;

  if (value === undefined) {
    updateConfig({
      params: Object.fromEntries(
        Object.entries(params).filter(([name]) => name !== key),
      ),
    });
  } else {
    updateConfig({ params: { ...params, [key]: value } });
  }
}

function parameter(key: string) {
  return computed({
    get: () => {
      const value = activeNode.value?.data.config.params?.[key];
      return typeof value === "string" ? value : undefined;
    },
    set: (value: string | undefined) => updateParameter(key, value),
  });
}

const topic = option("topic");
const matchPattern = option("match_pattern");
const schemaPath = option("schema_path");
const model = option("model");
const connector = option("connector");
const action = parameter("action");
const entityType = parameter("entity_type");
const cron = parameter("cron");
const lookback = parameter("lookback");
const threshold = computed({
  get: () => {
    const value = activeNode.value?.data.config.params?.threshold;
    return typeof value === "number" ? value : undefined;
  },
  set: (value: number | undefined) => updateParameter("threshold", value),
});

const regexError = computed(() => {
  if (activeNode.value?.data.stepType !== "source" || !matchPattern.value)
    return false;

  try {
    new RegExp(matchPattern.value);
    return false;
  } catch {
    return true;
  }
});

const thresholdError = computed(
  () =>
    activeNode.value?.data.stepType === "resolve" &&
    threshold.value !== undefined &&
    (threshold.value < 0 || threshold.value > 1),
);
</script>

<template>
  <NavbarAction :is-empty="!activeNode" @close="emit('close')">
    <template #header-badge>
      <span
        class="text-[10px] uppercase tracking-widest text-primary-foreground font-bold"
      >
        {{ activeNode?.data?.stepType }}
      </span>
    </template>

    <template #header-content>
      <h2 class="text-xl font-bold text-foreground leading-tight">
        {{ activeNode?.id }}
      </h2>
    </template>

    <template #default>
      <div
        v-if="activeNode?.data?.stepType === 'source' && activeNode.data.config"
        class="space-y-4"
      >
        <div>
          <label
            class="block text-muted font-bold mb-1.5 uppercase tracking-wider text-[10px]"
            >{{ $t("pipeline.config.source.topic") }}</label
          >
          <UiSelect
            v-model="topic"
            :options="[
              { label: 'raw-images'.trim(), value: 'raw-images' },
              { label: 'processed-events'.trim(), value: 'processed-events' },
              { label: 'telemetry-data'.trim(), value: 'telemetry-data' },
            ]"
            class="w-full"
          />
        </div>
        <div>
          <label
            class="block text-muted font-bold mb-1.5 uppercase tracking-wider text-[10px]"
            >{{ $t("pipeline.config.source.regex") }}</label
          >
          <UiInput
            v-model="matchPattern"
            placeholder="ex: ^images(/|%2F)"
            :class="['w-full', regexError ? '' : '']"
          />
          <p v-if="regexError" class="text-[10px] text-danger font-medium mt-1">
            {{ $t("pipeline.config.errors.invalid_regex") }}
          </p>
        </div>
        <div>
          <label
            class="block text-muted font-bold mb-1.5 uppercase tracking-wider text-[10px]"
            >{{ $t("pipeline.config.source.schema") }}</label
          >
          <UiSelect
            v-model="schemaPath"
            :options="[
              {
                label: ' schemas/avro/image.avsc '.trim(),
                value: 'schemas/avro/image.avsc',
              },
              {
                label: 'schemas/avro/log.avsc'.trim(),
                value: 'schemas/avro/log.avsc',
              },
              {
                label: ' schemas/avro/metric.avsc '.trim(),
                value: 'schemas/avro/metric.avsc',
              },
            ]"
            class="w-full"
          />
        </div>
      </div>

      <div
        v-if="
          activeNode?.data?.stepType === 'inference' && activeNode.data.config
        "
        class="space-y-4"
      >
        <div>
          <label
            class="block text-muted font-bold mb-1.5 uppercase tracking-wider text-[10px]"
            >{{ $t("pipeline.config.inference.model") }}</label
          >
          <UiSelect
            v-model="model"
            :options="[
              {
                label: ' FaceRecognitionModel '.trim(),
                value:
                  'galadril_inference.models.face_recognition.FaceRecognitionModel',
              },
              {
                label: ' YoloV8Model '.trim(),
                value: 'galadril_inference.models.object_detection.YoloV8Model',
              },
              {
                label: ' Llama3Model '.trim(),
                value: 'galadril_inference.models.text.Llama3Model',
              },
            ]"
            class="w-full"
          />
        </div>
        <div v-if="activeNode.data.config.params">
          <label
            class="block text-muted font-bold mb-1.5 uppercase tracking-wider text-[10px]"
            >{{ $t("pipeline.config.inference.action") }}</label
          >
          <UiSelect
            v-model="action"
            :options="[
              { label: 'embed'.trim(), value: 'embed' },
              { label: 'classify'.trim(), value: 'classify' },
              { label: 'detect'.trim(), value: 'detect' },
            ]"
            class="w-full"
          />
        </div>
      </div>

      <div
        v-if="
          activeNode?.data?.stepType === 'resolve' &&
          activeNode.data.config?.params
        "
        class="space-y-4"
      >
        <div>
          <label
            class="block text-muted font-bold mb-1.5 uppercase tracking-wider text-[10px]"
            >{{ $t("pipeline.config.resolve.threshold") }}</label
          >
          <UiInput
            v-model.number="threshold"
            type="number"
            min="0"
            max="1"
            step="0.01"
            :class="['w-full', thresholdError ? '' : '']"
          />
          <p
            v-if="thresholdError"
            class="text-[10px] text-danger font-medium mt-1"
          >
            {{ $t("pipeline.config.errors.invalid_threshold") }}
          </p>
        </div>
        <div>
          <label
            class="block text-muted font-bold mb-1.5 uppercase tracking-wider text-[10px]"
            >{{ $t("pipeline.config.resolve.entity_type") }}</label
          >
          <UiSelect
            v-model="entityType"
            :options="[
              { label: 'PERSON'.trim(), value: 'PERSON' },
              { label: 'VEHICLE'.trim(), value: 'VEHICLE' },
              { label: 'LOCATION'.trim(), value: 'LOCATION' },
            ]"
            class="w-full"
          />
        </div>
      </div>

      <div
        v-if="
          activeNode?.data?.stepType === 'causal' &&
          activeNode.data.config?.params
        "
        class="space-y-4"
      >
        <div
          v-if="!hasIncoming"
          class="p-3 bg-primary-subtle border border-border text-primary-foreground rounded-xl space-y-3 mb-2"
        >
          <span class="font-semibold block text-[11px]">{{
            $t("pipeline.config.causal.autonomous_plan")
          }}</span>
          <div>
            <label
              class="block text-primary-foreground font-medium mb-1 text-[10px]"
              >{{ $t("pipeline.config.causal.cron") }}</label
            >
            <UiInput v-model="cron" class="w-full" />
          </div>
        </div>
        <div
          v-else
          class="p-3 bg-surface-muted text-muted rounded-xl text-[11px] italic"
        >
          {{ $t("pipeline.config.causal.dynamic_trigger") }}
        </div>
        <div>
          <label
            class="block text-muted font-bold mb-1.5 uppercase tracking-wider text-[10px]"
            >{{ $t("pipeline.config.causal.lookback") }}</label
          >
          <UiSelect
            v-model="lookback"
            :options="[
              { label: '1d'.trim(), value: '1d' },
              { label: '7d'.trim(), value: '7d' },
              { label: '14d'.trim(), value: '14d' },
              { label: '30d'.trim(), value: '30d' },
            ]"
            class="w-full"
          />
        </div>
      </div>

      <div
        v-if="activeNode?.data?.stepType === 'sink' && activeNode.data.config"
        class="space-y-4"
      >
        <div>
          <label
            class="block text-muted font-bold mb-1.5 uppercase tracking-wider text-[10px]"
            >{{ $t("pipeline.config.sink.connector") }}</label
          >
          <UiSelect
            v-model="connector"
            disabled
            :options="[
              {
                label: String(
                  $t('pipeline.config.sink.postgres_active'),
                ).trim(),
                value: 'postgres',
              },
            ]"
            class="w-full"
          />
        </div>
      </div>
    </template>

    <template #footer>
      <UiButton
        :disabled="regexError || thresholdError"
        variant="ghost"
        :class="['w-full', regexError || thresholdError ? '' : '']"
        @click="emit('close')"
      >
        {{ $t("pipeline.config.save_close") }}
      </UiButton>
    </template>
  </NavbarAction>
</template>
