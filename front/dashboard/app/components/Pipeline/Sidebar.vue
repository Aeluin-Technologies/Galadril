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
        class="text-[10px] uppercase tracking-widest text-amber-600 font-bold"
      >
        {{ activeNode?.data?.stepType }}
      </span>
    </template>

    <template #header-content>
      <h2 class="text-xl font-bold text-zinc-950 leading-tight">
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
            class="block text-zinc-400 font-bold mb-1.5 uppercase tracking-wider text-[10px]"
            >{{ $t("pipeline.config.source.topic") }}</label
          >
          <select
            v-model="activeNode.data.config.topic"
            class="w-full bg-white border border-zinc-200 rounded-lg p-2.5 text-zinc-900 focus:border-amber-500 focus:outline-none shadow-sm transition"
          >
            <option value="raw-images">raw-images</option>
            <option value="processed-events">processed-events</option>
            <option value="telemetry-data">telemetry-data</option>
          </select>
        </div>
        <div>
          <label
            class="block text-zinc-400 font-bold mb-1.5 uppercase tracking-wider text-[10px]"
            >{{ $t("pipeline.config.source.regex") }}</label
          >
          <input
            v-model="activeNode.data.config.match_pattern"
            placeholder="ex: ^images(/|%2F)"
            :class="[
              'w-full bg-white border rounded-lg p-2.5 text-zinc-900 focus:outline-none shadow-sm transition',
              regexError
                ? 'border-red-500 focus:border-red-500 bg-red-50'
                : 'border-zinc-200 focus:border-amber-500',
            ]"
          />
          <p
            v-if="regexError"
            class="text-[10px] text-red-600 font-medium mt-1"
          >
            {{ $t("pipeline.config.errors.invalid_regex") }}
          </p>
        </div>
        <div>
          <label
            class="block text-zinc-400 font-bold mb-1.5 uppercase tracking-wider text-[10px]"
            >{{ $t("pipeline.config.source.schema") }}</label
          >
          <select
            v-model="activeNode.data.config.schema_path"
            class="w-full bg-white border border-zinc-200 rounded-lg p-2.5 text-zinc-900 focus:border-amber-500 focus:outline-none shadow-sm transition"
          >
            <option value="schemas/avro/image.avsc">
              schemas/avro/image.avsc
            </option>
            <option value="schemas/avro/log.avsc">schemas/avro/log.avsc</option>
            <option value="schemas/avro/metric.avsc">
              schemas/avro/metric.avsc
            </option>
          </select>
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
            class="block text-zinc-400 font-bold mb-1.5 uppercase tracking-wider text-[10px]"
            >{{ $t("pipeline.config.inference.model") }}</label
          >
          <select
            v-model="activeNode.data.config.model"
            class="w-full bg-white border border-zinc-200 rounded-lg p-2.5 text-zinc-900 focus:border-amber-500 focus:outline-none shadow-sm transition"
          >
            <option
              value="galadril_inference.models.face_recognition.FaceRecognitionModel"
            >
              FaceRecognitionModel
            </option>
            <option
              value="galadril_inference.models.object_detection.YoloV8Model"
            >
              YoloV8Model
            </option>
            <option value="galadril_inference.models.text.Llama3Model">
              Llama3Model
            </option>
          </select>
        </div>
        <div v-if="activeNode.data.config.params">
          <label
            class="block text-zinc-400 font-bold mb-1.5 uppercase tracking-wider text-[10px]"
            >{{ $t("pipeline.config.inference.action") }}</label
          >
          <select
            v-model="activeNode.data.config.params.action"
            class="w-full bg-white border border-zinc-200 rounded-lg p-2.5 text-zinc-900 focus:border-amber-500 focus:outline-none shadow-sm transition"
          >
            <option value="embed">embed</option>
            <option value="classify">classify</option>
            <option value="detect">detect</option>
          </select>
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
            class="block text-zinc-400 font-bold mb-1.5 uppercase tracking-wider text-[10px]"
            >{{ $t("pipeline.config.resolve.threshold") }}</label
          >
          <input
            type="number"
            min="0"
            max="1"
            step="0.01"
            v-model.number="activeNode.data.config.params.threshold"
            :class="[
              'w-full bg-white border rounded-lg p-2.5 text-zinc-900 focus:outline-none shadow-sm transition',
              thresholdError
                ? 'border-red-500 focus:border-red-500 bg-red-50'
                : 'border-zinc-200 focus:border-amber-500',
            ]"
          />
          <p
            v-if="thresholdError"
            class="text-[10px] text-red-600 font-medium mt-1"
          >
            {{ $t("pipeline.config.errors.invalid_threshold") }}
          </p>
        </div>
        <div>
          <label
            class="block text-zinc-400 font-bold mb-1.5 uppercase tracking-wider text-[10px]"
            >{{ $t("pipeline.config.resolve.entity_type") }}</label
          >
          <select
            v-model="activeNode.data.config.params.entity_type"
            class="w-full bg-white border border-zinc-200 rounded-lg p-2.5 text-zinc-900 focus:border-amber-500 focus:outline-none shadow-sm transition"
          >
            <option value="PERSON">PERSON</option>
            <option value="VEHICLE">VEHICLE</option>
            <option value="LOCATION">LOCATION</option>
          </select>
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
          class="p-3 bg-amber-50 border border-amber-200 text-amber-900 rounded-xl space-y-3 mb-2"
        >
          <span class="font-semibold block text-[11px]">{{
            $t("pipeline.config.causal.autonomous_plan")
          }}</span>
          <div>
            <label class="block text-amber-800 font-medium mb-1 text-[10px]">{{
              $t("pipeline.config.causal.cron")
            }}</label>
            <input
              v-model="activeNode.data.config.params.cron"
              class="w-full bg-white border border-amber-200 rounded-lg p-2 text-zinc-900 focus:border-amber-500 focus:outline-none transition text-xs"
            />
          </div>
        </div>
        <div
          v-else
          class="p-3 bg-zinc-100 text-zinc-500 rounded-xl text-[11px] italic"
        >
          {{ $t("pipeline.config.causal.dynamic_trigger") }}
        </div>
        <div>
          <label
            class="block text-zinc-400 font-bold mb-1.5 uppercase tracking-wider text-[10px]"
            >{{ $t("pipeline.config.causal.lookback") }}</label
          >
          <select
            v-model="activeNode.data.config.params.lookback"
            class="w-full bg-white border border-zinc-200 rounded-lg p-2.5 text-zinc-900 focus:border-amber-500 focus:outline-none shadow-sm transition"
          >
            <option value="1d">1d</option>
            <option value="7d">7d</option>
            <option value="14d">14d</option>
            <option value="30d">30d</option>
          </select>
        </div>
      </div>

      <div
        v-if="activeNode?.data?.stepType === 'sink' && activeNode.data.config"
        class="space-y-4"
      >
        <div>
          <label
            class="block text-zinc-400 font-bold mb-1.5 uppercase tracking-wider text-[10px]"
            >{{ $t("pipeline.config.sink.connector") }}</label
          >
          <select
            disabled
            v-model="activeNode.data.config.connector"
            class="w-full bg-zinc-100 border border-zinc-200 text-zinc-400 rounded-lg p-2.5 cursor-not-allowed shadow-sm"
          >
            <option value="postgres">
              {{ $t("pipeline.config.sink.postgres_active") }}
            </option>
          </select>
        </div>
      </div>
    </template>

    <template #footer>
      <button
        @click="emit('close')"
        :disabled="regexError || thresholdError"
        :class="[
          'w-full py-3.5 text-white rounded-xl transition font-semibold text-sm shadow-sm',
          regexError || thresholdError
            ? 'bg-zinc-400 cursor-not-allowed'
            : 'bg-zinc-950 hover:bg-zinc-800',
        ]"
      >
        {{ $t("pipeline.config.save_close") }}
      </button>
    </template>
  </NavbarAction>
</template>
