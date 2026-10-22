<script setup lang="ts">
const props = defineProps<{
  activeNode: any;
  hasIncoming: boolean;
}>();

const emit = defineEmits(["close"]);

const regexError = computed(() => {
  if (props.activeNode?.data?.stepType !== "source") return false;
  const pattern = props.activeNode.data.config?.match_pattern;
  if (!pattern) return false;
  try {
    new RegExp(pattern);
    return false;
  } catch (e) {
    return true;
  }
});

const thresholdError = computed(() => {
  if (props.activeNode?.data?.stepType !== "resolve") return false;
  const threshold = props.activeNode.data.config?.params?.threshold;
  return threshold < 0 || threshold > 1;
});
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
            v-model="activeNode.data.config.topic"
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
            v-model="activeNode.data.config.match_pattern"
            placeholder="ex: ^images(/|%2F)"
            :class="['w-full', regexError ? '' : '']"
          />
          <p
            v-if="regexError"
            class="text-[10px] text-danger font-medium mt-1"
          >
            {{ $t("pipeline.config.errors.invalid_regex") }}
          </p>
        </div>
        <div>
          <label
            class="block text-muted font-bold mb-1.5 uppercase tracking-wider text-[10px]"
            >{{ $t("pipeline.config.source.schema") }}</label
          >
          <UiSelect
            v-model="activeNode.data.config.schema_path"
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
            v-model="activeNode.data.config.model"
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
            v-model="activeNode.data.config.params.action"
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
            type="number"
            min="0"
            max="1"
            step="0.01"
            v-model.number="activeNode.data.config.params.threshold"
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
            v-model="activeNode.data.config.params.entity_type"
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
            <label class="block text-primary-foreground font-medium mb-1 text-[10px]">{{
              $t("pipeline.config.causal.cron")
            }}</label>
            <UiInput
              v-model="activeNode.data.config.params.cron"
              class="w-full"
            />
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
            v-model="activeNode.data.config.params.lookback"
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
            disabled
            v-model="activeNode.data.config.connector"
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
        @click="emit('close')"
        :disabled="regexError || thresholdError"
        variant="ghost"
        :class="['w-full', regexError || thresholdError ? '' : '']"
      >
        {{ $t("pipeline.config.save_close") }}
      </UiButton>
    </template>
  </NavbarAction>
</template>
