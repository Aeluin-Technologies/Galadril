<script setup lang="ts">
import { Primitive } from "reka-ui";
defineProps<{ active?: boolean }>();
</script>

<template>
  <Primitive
    as-child
    :aria-current="active ? 'page' : undefined"
    class="flex min-h-9 min-w-0 items-center gap-3 rounded-control px-3 py-2 text-start text-ui text-muted transition-colors hover:bg-surface-muted hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus aria-[current=page]:bg-primary-subtle aria-[current=page]:text-primary-foreground [&>svg]:size-4 [&>svg]:shrink-0"
  >
    <slot />
  </Primitive>
</template>
