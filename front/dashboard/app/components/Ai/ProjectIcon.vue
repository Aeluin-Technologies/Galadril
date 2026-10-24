<script setup lang="ts">
import { projectIcon } from "~/utils/project-icons";
const props = defineProps<{ name: string; color: string }>();
const selected = computed(() => projectIcon(props.name));
const color = computed(() =>
  /^#[a-fA-F0-9]{6}$/.test(props.color) ? props.color : undefined,
);
</script>
<template>
  <span
    :data-project-icon="selected.id"
    :style="{ color }"
    class="inline-flex shrink-0"
    aria-hidden="true"
    ><component :is="selected.icon" class="size-4"
  /></span>
</template>
