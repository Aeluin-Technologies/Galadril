<script setup>
import { ExclamationTriangleIcon } from "@heroicons/vue/24/outline";

defineProps({
  title: String,
  alerts: {
    type: Array,
    default: () => [],
  },
});
</script>

<template>
  <div class="bg-white border border-zinc-200 rounded-2xl flex flex-col">
    <div class="px-5 py-4 border-b border-zinc-100">
      <h3 class="text-sm font-semibold text-zinc-800 flex items-center">
        <ExclamationTriangleIcon class="w-4 h-4 mr-2 text-amber-600" />
        {{ title }}
      </h3>
    </div>
    <div class="flex-1 overflow-y-auto">
      <div
        v-for="alert in alerts"
        :key="alert.id"
        class="p-4 border-b border-zinc-50 hover:bg-stone-50 transition cursor-pointer group"
      >
        <div class="flex justify-between items-start mb-1">
          <span
            class="text-xs font-medium text-zinc-900 group-hover:text-amber-700"
          >
            {{ alert.title }}
          </span>
          <span class="text-[10px] text-zinc-400 font-mono">{{
            alert.time
          }}</span>
        </div>
        <p class="text-[11px] text-zinc-500 mb-2">{{ alert.meta }}</p>
        <span
          class="px-1.5 py-0.5 rounded text-[9px] font-bold uppercase border border-amber-200 bg-amber-50 text-amber-700"
        >
          {{ alert.status }}
        </span>
      </div>
    </div>
    <div class="p-3 bg-stone-50 text-center">
      <button
        class="text-[11px] font-medium text-zinc-500 hover:text-zinc-800 transition"
      >
        {{ $t("dashboard.alerts.open_manager") }}
      </button>
    </div>
  </div>
</template>
