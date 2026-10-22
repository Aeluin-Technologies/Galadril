<script setup lang="ts">
defineProps<{ label: string }>();
const emit = defineEmits<{ click: [event: MouseEvent] }>();
</script>

<template>
  <button
    type="button"
    data-ui="backdrop"
    :aria-label="label"
    class="absolute inset-0 bg-foreground/30 focus-visible:outline focus-visible:outline-2 focus-visible:outline-focus"
    @click="emit('click', $event)"
  />
</template>
