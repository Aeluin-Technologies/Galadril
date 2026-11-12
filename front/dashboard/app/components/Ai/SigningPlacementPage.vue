<script setup lang="ts">
import { ArrowsPointingOutIcon } from "@heroicons/vue/24/outline";
import type { PDFDocumentProxy, PageViewport, RenderTask } from "pdfjs-dist";
import type { SigningPlacement } from "~/utils/pdf-signing";

type Point = { x: number; y: number };
type Box = { x: number; y: number; width: number; height: number };
type Mode = "draw" | "move" | "resize";
const props = defineProps<{
  pdf: PDFDocumentProxy;
  page: number;
  editable: boolean;
}>();
const placement = defineModel<SigningPlacement>();
const surface = useTemplateRef<HTMLDivElement>("surface");
const canvas = useTemplateRef<HTMLCanvasElement>("canvas");
const viewport = shallowRef<PageViewport>();
const error = ref(false);
const gesture = shallowRef<{ mode: Mode; origin: Point; box?: Box }>();
const draft = shallowRef<Box>();
let observer: IntersectionObserver | undefined;
let render: RenderTask | undefined;
let epoch = 0;

async function paint() {
  const target = canvas.value;
  if (!target) return;
  const own = ++epoch;
  error.value = false;
  try {
    const page = await props.pdf.getPage(props.page + 1);
    if (own !== epoch) return;
    const base = page.getViewport({ scale: 1 });
    const view = page.getViewport({
      scale: 900 / Math.max(base.width, base.height),
    });
    viewport.value = view;
    target.width = Math.ceil(view.width);
    target.height = Math.ceil(view.height);
    render = page.render({ canvas: target, viewport: view });
    await render.promise;
  } catch {
    if (own === epoch) error.value = true;
  }
}

onMounted(() => {
  if (!surface.value) return;
  observer = new IntersectionObserver(
    ([entry]) => {
      if (entry?.isIntersecting) {
        void paint();
      } else {
        epoch++;
        render?.cancel();
        if (canvas.value) {
          canvas.value.width = 0;
          canvas.value.height = 0;
        }
      }
    },
    { rootMargin: "250px" },
  );
  observer.observe(surface.value);
});
onBeforeUnmount(() => {
  epoch++;
  observer?.disconnect();
  render?.cancel();
});

const box = computed<Box | undefined>(() => {
  const view = viewport.value;
  if (!view || placement.value?.page !== props.page) return;
  const [a, b, c, d] = view.convertToViewportRectangle([
    ...placement.value.rect,
  ]);
  if (a === undefined || b === undefined || c === undefined || d === undefined)
    return;
  return {
    x: Math.min(a, c) / view.width,
    y: Math.min(b, d) / view.height,
    width: Math.abs(c - a) / view.width,
    height: Math.abs(d - b) / view.height,
  };
});
const clamp = (value: number, maximum = 1) =>
  Math.max(0, Math.min(maximum, value));

function position(event: PointerEvent): Point {
  const area = surface.value?.getBoundingClientRect();
  return area
    ? {
        x: clamp((event.clientX - area.left) / area.width),
        y: clamp((event.clientY - area.top) / area.height),
      }
    : { x: 0, y: 0 };
}

function place(area: Box) {
  const view = viewport.value;
  if (
    !view ||
    (area.width * view.width) / view.scale < 8 ||
    (area.height * view.height) / view.scale < 8
  )
    return;
  const [ax, ay] = view.convertToPdfPoint(
    area.x * view.width,
    area.y * view.height,
  );
  const [bx, by] = view.convertToPdfPoint(
    (area.x + area.width) * view.width,
    (area.y + area.height) * view.height,
  );
  if (
    ax === undefined ||
    ay === undefined ||
    bx === undefined ||
    by === undefined
  )
    return;
  placement.value = {
    page: props.page,
    rect: [
      Math.min(ax, bx),
      Math.min(ay, by),
      Math.max(ax, bx),
      Math.max(ay, by),
    ],
  };
}

function start(event: PointerEvent, mode: Mode) {
  if (
    !viewport.value ||
    !event.isPrimary ||
    event.button !== 0 ||
    (mode !== "draw" && !box.value)
  )
    return;
  surface.value?.setPointerCapture(event.pointerId);
  gesture.value = { mode, origin: position(event), box: box.value };
}

function transform(
  area: Box,
  mode: "move" | "resize",
  dx: number,
  dy: number,
): Box {
  if (mode === "move")
    return {
      ...area,
      x: clamp(area.x + dx, 1 - area.width),
      y: clamp(area.y + dy, 1 - area.height),
    };
  return {
    ...area,
    width: Math.max(
      Math.min(0.06, 1 - area.x),
      clamp(area.width + dx, 1 - area.x),
    ),
    height: Math.max(
      Math.min(0.035, 1 - area.y),
      clamp(area.height + dy, 1 - area.y),
    ),
  };
}

function move(event: PointerEvent) {
  const active = gesture.value;
  if (!active || !event.isPrimary) return;
  const point = position(event);
  const dx = point.x - active.origin.x;
  const dy = point.y - active.origin.y;
  if (active.mode === "draw")
    draft.value = {
      x: Math.min(point.x, active.origin.x),
      y: Math.min(point.y, active.origin.y),
      width: Math.abs(dx),
      height: Math.abs(dy),
    };
  else if (active.box) draft.value = transform(active.box, active.mode, dx, dy);
}

function cancel() {
  gesture.value = undefined;
  draft.value = undefined;
}

function at(point: Point) {
  place({
    x: clamp(point.x - 0.11, 0.78),
    y: clamp(point.y - 0.035, 0.93),
    width: 0.22,
    height: 0.07,
  });
}

function finish(event: PointerEvent) {
  const active = gesture.value;
  if (!active || !event.isPrimary) return;
  move(event);
  const area = draft.value;
  cancel();
  if (!area) return;
  if (active.mode === "draw" && area.width < 0.02 && area.height < 0.02)
    at(active.origin);
  else place(area);
}

function adjust(event: KeyboardEvent, mode: "move" | "resize") {
  const area = box.value;
  const delta = {
    ArrowLeft: [-0.015, 0],
    ArrowRight: [0.015, 0],
    ArrowUp: [0, -0.015],
    ArrowDown: [0, 0.015],
  }[event.key];
  if (!area || !delta) return;
  event.preventDefault();
  place(transform(area, mode, delta[0] ?? 0, delta[1] ?? 0));
}
const overlay = computed(() => {
  const area = draft.value ?? box.value;
  return (
    area && {
      left: `${area.x * 100}%`,
      top: `${area.y * 100}%`,
      width: `${area.width * 100}%`,
      height: `${area.height * 100}%`,
    }
  );
});
</script>

<template>
  <div
    ref="surface"
    :data-signing-page="page"
    class="relative bg-white shadow-panel"
    :style="{
      aspectRatio: viewport ? viewport.width / viewport.height : 0.707,
    }"
    @pointermove="move"
    @pointerup="finish"
    @pointercancel="cancel"
    @lostpointercapture="cancel"
  >
    <canvas
      ref="canvas"
      class="absolute inset-0 size-full"
      aria-hidden="true"
    />
    <button
      v-if="editable"
      type="button"
      class="absolute inset-0 touch-pan-y cursor-crosshair focus-visible:outline-focus"
      :disabled="!viewport"
      :aria-label="$t('documentSigning.placeOnPage', { page: page + 1 })"
      @pointerdown="start($event, 'draw')"
      @click="$event.detail === 0 && at({ x: 0.5, y: 0.5 })"
    />
    <div
      v-if="editable && overlay"
      class="absolute border-2 border-primary bg-primary/15"
      :style="overlay"
    >
      <button
        type="button"
        class="absolute inset-0 size-full touch-none cursor-move focus-visible:outline-focus"
        :aria-label="$t('documentSigning.movePlacement')"
        @pointerdown.stop="start($event, 'move')"
        @keydown="adjust($event, 'move')"
      />
      <button
        type="button"
        class="absolute -bottom-3 -right-3 grid size-7 touch-none cursor-nwse-resize place-items-center rounded-control border border-primary bg-surface text-foreground focus-visible:outline-focus"
        :aria-label="$t('documentSigning.resizePlacement')"
        @pointerdown.stop="start($event, 'resize')"
        @keydown="adjust($event, 'resize')"
      >
        <ArrowsPointingOutIcon class="size-4" aria-hidden="true" />
      </button>
    </div>
    <UiAlert v-if="error" class="relative" tone="danger">{{
      $t("documentSigning.unavailable")
    }}</UiAlert>
  </div>
</template>
