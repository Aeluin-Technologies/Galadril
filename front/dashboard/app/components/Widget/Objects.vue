<script setup>
import { ArrowUpRightIcon } from "@heroicons/vue/24/outline";

defineProps({
  title: String,
  items: {
    type: Array,
    default: () => [],
  },
});
</script>

<template>
  <div class="bg-surface border border-border rounded-2xl p-6">
    <div class="flex items-center justify-between mb-6">
      <h3 class="text-sm font-semibold text-foreground">
        {{ title }}
      </h3>
    </div>
    <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
      <div v-for="item in items" :key="item.id" class="relative group">
        <WidgetCard
          :title="item.id"
          :value="item.name"
          :icon="item.icon"
          trend="Stable"
          size="sm"
          class="cursor-pointer border-border hover:border-border"
        />
        <ArrowUpRightIcon
          class="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted group-hover:text-primary-foreground transition-colors pointer-events-none"
        />
      </div>
    </div>
  </div>
</template>
