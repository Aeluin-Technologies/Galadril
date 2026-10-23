<script setup lang="ts">
defineProps<{ title: string; description?: string; eyebrow?: string }>();
</script>

<template>
  <header
    data-ui="page-header"
    class="flex flex-wrap items-end justify-between gap-5"
  >
    <div>
      <p
        v-if="eyebrow"
        class="mb-2 text-ui-meta font-semibold uppercase tracking-widest text-muted"
      >
        {{ eyebrow }}
      </p>
      <h1 class="text-ui-title font-semibold tracking-tight text-foreground">
        {{ title }}
      </h1>
      <p v-if="description" class="mt-2 max-w-2xl text-ui text-muted">
        {{ description }}
      </p>
    </div>
    <div v-if="$slots.actions" class="flex flex-wrap items-center gap-3">
      <slot name="actions" />
    </div>
  </header>
</template>
