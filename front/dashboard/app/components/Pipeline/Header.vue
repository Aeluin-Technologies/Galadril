<script setup lang="ts">
import {
  PlusIcon,
  ArrowDownTrayIcon,
  CircleStackIcon,
  CpuChipIcon,
  AdjustmentsHorizontalIcon,
  ArrowDownOnSquareIcon,
  BoltIcon,
} from "@heroicons/vue/24/outline";

const props = defineProps<{
  modelValue: string;
}>();

const emit = defineEmits<{
  (e: "update:modelValue", value: string): void;
  (e: "export"): void;
  (
    e: "add-step",
    type: "source" | "inference" | "resolve" | "sink" | "causal",
  ): void;
}>();
</script>

<template>
  <header class="space-y-4">
    <div class="flex items-center justify-between">
      <UiInput
        :value="props.modelValue"
        :placeholder="$t('pipeline.header.pipeline_name')"
        class="w-1/2"
        @input="
          emit('update:modelValue', ($event.target as HTMLInputElement).value)
        "
      />

      <div class="flex items-center gap-3">
        <UiButton variant="secondary" @click="emit('export')">
          <ArrowDownTrayIcon class="w-4 h-4 mr-2" />
          {{ $t("pipeline.header.save") }}
        </UiButton>

        <UtilsDropdown>
          <template #trigger>
            <UiButton variant="primary">
              <PlusIcon class="w-4 h-4 mr-2" />
              {{ $t("pipeline.header.add_module") }}
            </UiButton>
          </template>
          <UiButton
            variant="secondary"
            class="flex items-center w-full"
            @click="emit('add-step', 'source')"
          >
            <CircleStackIcon class="w-4 h-4 mr-2 text-muted" />
            {{ $t("pipeline.modules.source") }}
          </UiButton>
          <UiButton
            variant="secondary"
            class="flex items-center w-full"
            @click="emit('add-step', 'inference')"
          >
            <CpuChipIcon class="w-4 h-4 mr-2 text-muted" />
            {{ $t("pipeline.modules.inference") }}
          </UiButton>
          <UiButton
            variant="secondary"
            class="flex items-center w-full"
            @click="emit('add-step', 'resolve')"
          >
            <AdjustmentsHorizontalIcon class="w-4 h-4 mr-2 text-muted" />
            {{ $t("pipeline.modules.resolve") }}
          </UiButton>
          <UiButton
            variant="secondary"
            class="flex items-center w-full"
            @click="emit('add-step', 'sink')"
          >
            <ArrowDownOnSquareIcon class="w-4 h-4 mr-2 text-muted" />
            {{ $t("pipeline.modules.sink") }}
          </UiButton>
          <UiButton
            variant="ghost"
            class="flex items-center w-full"
            @click="emit('add-step', 'causal')"
          >
            <BoltIcon class="w-4 h-4 mr-2 text-muted" />
            {{ $t("pipeline.modules.causal") }}
          </UiButton>
        </UtilsDropdown>
      </div>
    </div>
  </header>
</template>
