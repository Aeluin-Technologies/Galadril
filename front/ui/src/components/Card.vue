<script setup lang="ts">
import { useId } from "vue";
withDefaults(
  defineProps<{
    title?: string;
    description?: string;
    as?: "section" | "article" | "form" | "div";
  }>(),
  { as: "section" },
);
const id = useId();
</script>

<template>
  <component
    :is="as"
    data-ui="card"
    :aria-labelledby="title ? id : undefined"
    class="min-w-0 rounded-panel border border-border bg-surface text-foreground shadow-panel"
  >
    <header
      v-if="title || $slots.actions"
      class="flex flex-wrap items-start justify-between gap-4 border-b border-border p-5"
    >
      <div>
        <h2 v-if="title" :id="id" class="text-ui-section font-semibold">
          {{ title }}
        </h2>
        <p v-if="description" class="mt-1 text-ui text-muted">
          {{ description }}
        </p>
      </div>
      <slot name="actions" />
    </header>
    <div class="p-5"><slot /></div>
    <footer v-if="$slots.footer" class="border-t border-border p-5">
      <slot name="footer" />
    </footer>
  </component>
</template>
