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
      <input
        :value="props.modelValue"
        @input="
          emit('update:modelValue', ($event.target as HTMLInputElement).value)
        "
        class="text-3xl font-bold text-zinc-900 bg-transparent border-b-2 border-transparent hover:border-zinc-300 focus:border-amber-500 focus:outline-none transition-colors pb-1 w-1/2"
        :placeholder="$t('pipeline.header.pipeline_name')"
      />

      <div class="flex items-center gap-3">
        <UtilsButton variant="secondary" @click="emit('export')">
          <ArrowDownTrayIcon class="w-4 h-4 mr-2" />
          {{ $t("pipeline.header.save") }}
        </UtilsButton>

        <UtilsDropdown>
          <template #trigger>
            <UtilsButton variant="primary">
              <PlusIcon class="w-4 h-4 mr-2" />
              {{ $t("pipeline.header.add_module") }}
            </UtilsButton>
          </template>
          <button
            @click="emit('add-step', 'source')"
            class="flex items-center w-full text-left px-4 py-2 text-sm text-zinc-700 hover:bg-stone-50 border-b border-zinc-100"
          >
            <CircleStackIcon class="w-4 h-4 mr-2 text-zinc-400" />
            {{ $t("pipeline.modules.source") }}
          </button>
          <button
            @click="emit('add-step', 'inference')"
            class="flex items-center w-full text-left px-4 py-2 text-sm text-zinc-700 hover:bg-stone-50 border-b border-zinc-100"
          >
            <CpuChipIcon class="w-4 h-4 mr-2 text-zinc-400" />
            {{ $t("pipeline.modules.inference") }}
          </button>
          <button
            @click="emit('add-step', 'resolve')"
            class="flex items-center w-full text-left px-4 py-2 text-sm text-zinc-700 hover:bg-stone-50 border-b border-zinc-100"
          >
            <AdjustmentsHorizontalIcon class="w-4 h-4 mr-2 text-zinc-400" />
            {{ $t("pipeline.modules.resolve") }}
          </button>
          <button
            @click="emit('add-step', 'sink')"
            class="flex items-center w-full text-left px-4 py-2 text-sm text-zinc-700 hover:bg-stone-50 border-b border-zinc-100"
          >
            <ArrowDownOnSquareIcon class="w-4 h-4 mr-2 text-zinc-400" />
            {{ $t("pipeline.modules.sink") }}
          </button>
          <button
            @click="emit('add-step', 'causal')"
            class="flex items-center w-full text-left px-4 py-2 text-sm text-zinc-700 hover:bg-stone-50"
          >
            <BoltIcon class="w-4 h-4 mr-2 text-zinc-400" />
            {{ $t("pipeline.modules.causal") }}
          </button>
        </UtilsDropdown>
      </div>
    </div>
  </header>
</template>
