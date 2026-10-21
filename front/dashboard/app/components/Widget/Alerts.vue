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
  <div class="bg-surface border border-border rounded-2xl flex flex-col">
    <div class="px-5 py-4 border-b border-border">
      <h3 class="text-sm font-semibold text-foreground flex items-center">
        <ExclamationTriangleIcon class="w-4 h-4 mr-2 text-primary-foreground" />
        {{ title }}
      </h3>
    </div>
    <div class="flex-1 overflow-y-auto">
      <div
        v-for="alert in alerts"
        :key="alert.id"
        class="p-4 border-b border-border hover:bg-canvas transition cursor-pointer group"
      >
        <div class="flex justify-between items-start mb-1">
          <span
            class="text-xs font-medium text-foreground group-hover:text-primary-foreground"
          >
            {{ alert.title }}
          </span>
          <span class="text-[10px] text-muted font-mono">{{
            alert.time
          }}</span>
        </div>
        <p class="text-[11px] text-muted mb-2">{{ alert.meta }}</p>
        <span
          class="px-1.5 py-0.5 rounded text-[9px] font-bold uppercase border border-border bg-primary-subtle text-primary-foreground"
        >
          {{ alert.status }}
        </span>
      </div>
    </div>
    <div class="p-3 bg-canvas text-center">
      <UiButton variant="ghost">
        {{ $t("dashboard.alerts.open_manager") }}
      </UiButton>
    </div>
  </div>
</template>
