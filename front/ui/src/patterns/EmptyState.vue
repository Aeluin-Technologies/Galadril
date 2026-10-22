<script setup lang="ts">
import { MagnifyingGlassIcon } from "@heroicons/vue/24/outline";
defineProps<{ title: string; description: string }>();
</script>

<template>
  <div
    data-ui="empty-state"
    class="grid justify-items-center gap-3 py-12 text-center"
  >
    <MagnifyingGlassIcon class="size-8 text-muted" aria-hidden="true" />
    <h3 class="text-ui-section font-semibold text-foreground">{{ title }}</h3>
    <p class="max-w-sm text-ui text-muted">{{ description }}</p>
    <div v-if="$slots.default" class="mt-2"><slot /></div>
  </div>
</template>
