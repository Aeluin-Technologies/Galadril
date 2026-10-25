<script setup lang="ts">
import { Primitive } from "reka-ui";
withDefaults(
  defineProps<{ active?: boolean; variant?: "default" | "section" }>(),
  { variant: "default" },
);
</script>

<template>
  <Primitive
    as-child
    :aria-current="active ? 'page' : undefined"
    :class="[
      'min-w-0 text-start text-muted transition-colors hover:bg-surface-muted focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus aria-[current=page]:bg-primary-subtle aria-[current=page]:text-primary-foreground',
      variant === 'section'
        ? 'block shrink-0 rounded-xl px-4 py-3 text-sm aria-[current=page]:ring-1 aria-[current=page]:ring-inset aria-[current=page]:ring-focus'
        : 'flex min-h-9 items-center gap-3 rounded-control px-3 py-2 text-ui hover:text-foreground [&>svg]:size-4 [&>svg]:shrink-0',
    ]"
  >
    <slot />
  </Primitive>
</template>
