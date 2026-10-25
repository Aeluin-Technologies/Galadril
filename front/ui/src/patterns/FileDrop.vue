<script setup lang="ts">
import {
  onMounted,
  onActivated,
  onDeactivated,
  onBeforeUnmount,
  ref,
  watch,
} from "vue";
import { ArrowUpTrayIcon } from "@heroicons/vue/24/outline";
import { FileDragSession } from "../file-drag";
const props = defineProps<{
  label: string;
  description?: string;
  disabled?: boolean;
}>();
const emit = defineEmits<{ files: [files: File[]] }>();
const session = new FileDragSession();
const dragging = ref(false);
let active = false;
function reset() {
  session.reset();
  dragging.value = false;
}
function hasFiles(event: DragEvent) {
  return event.dataTransfer?.types.includes("Files") === true;
}
function protect(event: DragEvent) {
  if (active && (hasFiles(event) || session.active)) event.preventDefault();
}
function enter(event: DragEvent) {
  if (props.disabled) return;
  dragging.value = session.enter(event.dataTransfer?.types ?? []);
  if (dragging.value) event.preventDefault();
}
function over(event: DragEvent) {
  if (!hasFiles(event) && !session.active) return;
  event.preventDefault();
  if (event.dataTransfer)
    event.dataTransfer.dropEffect = props.disabled ? "none" : "copy";
  if (!props.disabled && !session.active)
    dragging.value = session.enter(event.dataTransfer?.types ?? []);
}
function leave() {
  dragging.value = session.leave();
}
function drop(event: DragEvent) {
  if (!hasFiles(event) && !session.active) return;
  event.preventDefault();
  event.stopPropagation();
  reset();
  if (props.disabled) return;
  const files = Array.from(event.dataTransfer?.files ?? []);
  if (files.length) emit("files", files);
}
function outsideDrop(event: DragEvent) {
  protect(event);
  reset();
}
function keydown(event: KeyboardEvent) {
  if (event.key === "Escape") reset();
}
function start() {
  if (active) return;
  active = true;
  window.addEventListener("dragover", protect);
  window.addEventListener("drop", outsideDrop);
  window.addEventListener("dragend", reset);
  window.addEventListener("blur", reset);
  window.addEventListener("keydown", keydown);
}
function stop() {
  active = false;
  reset();
  window.removeEventListener("dragover", protect);
  window.removeEventListener("drop", outsideDrop);
  window.removeEventListener("dragend", reset);
  window.removeEventListener("blur", reset);
  window.removeEventListener("keydown", keydown);
}
watch(
  () => props.disabled,
  () => {
    if (props.disabled) reset();
  },
);
onMounted(start);
onActivated(start);
onDeactivated(stop);
onBeforeUnmount(stop);
</script>

<template>
  <div
    data-file-drop
    class="relative"
    @dragenter="enter"
    @dragover="over"
    @dragleave="leave"
    @drop="drop"
  >
    <slot />
    <div
      v-if="dragging"
      data-file-drop-overlay
      role="status"
      :aria-label="label"
      aria-live="polite"
      aria-atomic="true"
      class="pointer-events-none absolute inset-3 z-40 grid place-content-center gap-3 rounded-panel border-2 border-dashed border-focus bg-surface/95 p-6 text-center text-foreground shadow-panel"
    >
      <ArrowUpTrayIcon
        class="mx-auto size-10 text-primary-foreground"
        aria-hidden="true"
      />
      <strong class="text-ui-section">{{ label }}</strong>
      <span v-if="description" class="text-ui text-muted">{{
        description
      }}</span>
    </div>
  </div>
</template>
