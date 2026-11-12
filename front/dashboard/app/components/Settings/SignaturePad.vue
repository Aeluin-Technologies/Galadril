<script setup lang="ts">
import { signaturePadAspectRatio } from "~/utils/signature-ink";
type Point = [number, number];
const strokes = defineModel<Point[][]>({ required: true });
defineProps<{ disabled: boolean }>();
const cursor = ref<Point>([500, 500]);
const active = ref(false);
let pointer: number | undefined;

function bounded(value: number): number {
  return Math.max(0, Math.min(1000, Math.round(value)));
}

function start(point: Point): void {
  if (strokes.value.length >= 32) return;
  strokes.value = [...strokes.value, [point]];
  active.value = true;
}

function append(point: Point): void {
  cursor.value = point;
  if (!active.value) return;
  const stroke = strokes.value.at(-1);
  const last = stroke?.at(-1);
  if (!stroke || (last?.[0] === point[0] && last[1] === point[1])) return;
  if (strokes.value.reduce((count, path) => count + path.length, 0) >= 1024)
    return;
  strokes.value = [...strokes.value.slice(0, -1), [...stroke, point]];
}

function finish(): void {
  if (active.value && strokes.value.at(-1)?.length === 1)
    strokes.value = strokes.value.slice(0, -1);
  active.value = false;
  pointer = undefined;
}

function pointAt(event: PointerEvent): Point | undefined {
  const target = event.currentTarget;
  if (!(target instanceof SVGSVGElement)) return;
  const rect = target.getBoundingClientRect();
  return [
    bounded(((event.clientX - rect.left) / rect.width) * 1000),
    bounded(((event.clientY - rect.top) / rect.height) * 1000),
  ];
}

function pointerDown(event: PointerEvent): void {
  if (pointer !== undefined || event.button !== 0) return;
  const point = pointAt(event);
  const target = event.currentTarget;
  if (!point || !(target instanceof SVGSVGElement)) return;
  finish();
  start(point);
  pointer = event.pointerId;
  target.setPointerCapture(pointer);
}

function pointerMove(event: PointerEvent): void {
  if (pointer !== event.pointerId) return;
  const point = pointAt(event);
  if (point) append(point);
}

function pointerEnd(event: PointerEvent): void {
  if (pointer === event.pointerId) finish();
}

function keyboard(event: KeyboardEvent): void {
  if (pointer !== undefined) return;
  if (event.key === " ") {
    event.preventDefault();
    if (event.repeat) return;
    if (active.value) finish();
    else start(cursor.value);
    return;
  }
  if (event.key === "Escape") {
    finish();
    return;
  }
  const [x, y] = cursor.value;
  const step = event.shiftKey ? 5 : 25;
  switch (event.key) {
    case "ArrowLeft":
      append([bounded(x - step), y]);
      break;
    case "ArrowRight":
      append([bounded(x + step), y]);
      break;
    case "ArrowUp":
      append([x, bounded(y - step)]);
      break;
    case "ArrowDown":
      append([x, bounded(y + step)]);
      break;
    default:
      return;
  }
  event.preventDefault();
}
</script>

<template>
  <svg
    role="img"
    :aria-label="$t('signatureSettings.pad')"
    aria-describedby="signature-pad-help"
    :tabindex="disabled ? -1 : 0"
    viewBox="0 0 1000 1000"
    preserveAspectRatio="none"
    :style="{ aspectRatio: signaturePadAspectRatio }"
    class="group block w-full touch-none rounded-control border border-border-control bg-surface text-foreground focus-visible:outline-focus"
    @pointerdown="!disabled && pointerDown($event)"
    @pointermove="!disabled && pointerMove($event)"
    @pointerup="pointerEnd"
    @pointercancel="pointerEnd"
    @lostpointercapture="pointerEnd"
    @keydown="!disabled && keyboard($event)"
    @blur="finish"
  >
    <polyline
      v-for="(stroke, index) in strokes"
      :key="index"
      :points="stroke.map((point) => point.join(',')).join(' ')"
      vector-effect="non-scaling-stroke"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
    />
    <ellipse
      :cx="cursor[0]"
      :cy="cursor[1]"
      rx="6"
      ry="16"
      fill="currentColor"
      class="invisible group-focus-visible:visible"
    />
  </svg>
  <p id="signature-pad-help" class="text-sm text-muted">
    {{ $t("signatureSettings.padHelp") }}
  </p>
</template>
