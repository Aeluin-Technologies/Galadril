<script setup lang="ts">
import { MagnifyingGlassIcon } from "@heroicons/vue/20/solid";
defineProps<{ label: string; shortcut?: string }>();
const emit = defineEmits<{ click: [event: MouseEvent] }>();
</script>

<template>
  <button
    type="button"
    data-ui="search-trigger"
    :aria-label="label"
    class="inline-flex h-9 min-w-0 items-center gap-3 rounded-control border border-border bg-canvas px-2 text-ui sm:px-3 text-muted transition-colors hover:bg-surface-muted hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
    @click="emit('click', $event)"
  >
    <MagnifyingGlassIcon class="size-4 shrink-0" aria-hidden="true" />
    <span class="hidden min-w-0 flex-1 truncate text-start sm:block">{{
      label
    }}</span>
    <kbd
      v-if="shortcut"
      aria-hidden="true"
      class="hidden shrink-0 rounded border border-border bg-surface px-1.5 py-0.5 font-sans text-ui-meta sm:block"
      >{{ shortcut }}</kbd
    >
  </button>
</template>
