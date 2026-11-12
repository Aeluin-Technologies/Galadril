<script setup lang="ts">
import {
  getDocument,
  GlobalWorkerOptions,
  type PageViewport,
  type RenderTask,
} from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import type { SigningPlacement } from "~/utils/pdf-signing";

const props = defineProps<{ bytes: Uint8Array<ArrayBuffer> }>();
const placement = defineModel<SigningPlacement>();
const { t } = useI18n();
const canvas = useTemplateRef<HTMLCanvasElement>("canvas");
const page = ref(placement.value?.page ?? 0);
const count = ref(0);
const viewport = shallowRef<PageViewport>();
const error = ref(false);
const loading = ref(true);
const gesture = ref<{ x: number; y: number }>();
const drag = ref<{ x: number; y: number }>();
let render: RenderTask | undefined;
let epoch = 0;
GlobalWorkerOptions.workerSrc = workerUrl;
const documentTask = getDocument({
  data: props.bytes.slice(),
  enableXfa: false,
});

async function showPage() {
  const target = canvas.value;
  if (!target) return;
  const own = ++epoch;
  render?.cancel();
  viewport.value = undefined;
  loading.value = true;
  error.value = false;
  try {
    const pdf = await documentTask.promise;
    count.value = pdf.numPages;
    const item = await pdf.getPage(page.value + 1);
    if (own !== epoch) return;
    const size = item.getViewport({ scale: 1 });
    const view = item.getViewport({
      scale: 840 / Math.max(size.width, size.height),
    });
    target.width = Math.ceil(view.width);
    target.height = Math.ceil(view.height);
    render = item.render({ canvas: target, viewport: view });
    await render.promise;
    if (own === epoch) viewport.value = view;
  } catch {
    if (own === epoch) error.value = true;
  } finally {
    if (own === epoch) loading.value = false;
  }
}

onMounted(showPage);
watch(page, showPage);
watch(
  () => placement.value?.page,
  (next) => {
    if (next !== undefined) page.value = next;
  },
);
onBeforeUnmount(() => {
  epoch++;
  render?.cancel();
  void documentTask.destroy().catch(() => {});
});

function position(event: PointerEvent) {
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
  return {
    x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
    y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)),
  };
}

function place(x: number, y: number, right: number, bottom: number) {
  const view = viewport.value;
  if (!view) return;
  const [ax, ay] = view.convertToPdfPoint(x * view.width, y * view.height);
  const [bx, by] = view.convertToPdfPoint(
    right * view.width,
    bottom * view.height,
  );
  if (
    ax === undefined ||
    ay === undefined ||
    bx === undefined ||
    by === undefined ||
    Math.abs(ax - bx) < 8 ||
    Math.abs(ay - by) < 8
  )
    return;
  placement.value = {
    page: page.value,
    rect: [
      Math.min(ax, bx),
      Math.min(ay, by),
      Math.max(ax, bx),
      Math.max(ay, by),
    ],
  };
}

function defaultPlacement(x = 0.5, y = 0.5) {
  const left = Math.min(0.7, Math.max(0, x - 0.15));
  const top = Math.min(0.9, Math.max(0, y - 0.05));
  place(left, top, left + 0.3, top + 0.1);
}

function start(event: PointerEvent) {
  if (!viewport.value || !event.isPrimary || event.button !== 0) return;
  (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
  gesture.value = position(event);
  drag.value = gesture.value;
}

function move(event: PointerEvent) {
  if (gesture.value && event.isPrimary) drag.value = position(event);
}

function cancel() {
  gesture.value = undefined;
  drag.value = undefined;
}

function finish(event: PointerEvent) {
  const start = gesture.value;
  if (!start || !event.isPrimary) return;
  const end = position(event);
  cancel();
  if (Math.abs(start.x - end.x) < 0.02 && Math.abs(start.y - end.y) < 0.02)
    defaultPlacement(end.x, end.y);
  else place(start.x, start.y, end.x, end.y);
}

const bounds = computed(() => {
  const view = viewport.value;
  if (!view) return;
  if (gesture.value && drag.value)
    return [gesture.value.x, gesture.value.y, drag.value.x, drag.value.y];
  if (placement.value?.page !== page.value) return;
  const [a, b, c, d] = view.convertToViewportRectangle([
    ...placement.value.rect,
  ]);
  if (a === undefined || b === undefined || c === undefined || d === undefined)
    return;
  return [a / view.width, b / view.height, c / view.width, d / view.height];
});

const overlay = computed(() => {
  const [a = 0, b = 0, c = 0, d = 0] = bounds.value ?? [];
  return {
    left: `${Math.min(a, c) * 100}%`,
    top: `${Math.min(b, d) * 100}%`,
    width: `${Math.abs(a - c) * 100}%`,
    height: `${Math.abs(b - d) * 100}%`,
  };
});
</script>

<template>
  <div class="space-y-2">
    <div class="flex flex-wrap items-center gap-2">
      <UiButton variant="secondary" :disabled="page === 0" @click="page--">{{
        t("documentSigning.previousPage")
      }}</UiButton>
      <span v-if="count">{{
        t("documentSigning.page", { page: page + 1, count })
      }}</span>
      <UiButton
        variant="secondary"
        :disabled="page + 1 >= count"
        @click="page++"
        >{{ t("documentSigning.nextPage") }}</UiButton
      >
      <UiButton
        variant="secondary"
        :disabled="!viewport"
        @click="defaultPlacement()"
        >{{ t("documentSigning.center") }}</UiButton
      >
    </div>
    <p v-if="loading" role="status">{{ t("documentSigning.loading") }}</p>
    <UiAlert v-if="error" tone="danger">{{
      t("documentSigning.unavailable")
    }}</UiAlert>
    <div class="max-h-[50vh] overflow-auto">
      <div
        class="relative mx-auto w-fit max-w-full"
        @pointerdown="start"
        @pointermove="move"
        @pointerup="finish"
        @pointercancel="cancel"
        @lostpointercapture="cancel"
      >
        <canvas
          ref="canvas"
          :aria-label="t('documentSigning.placement')"
          class="block max-w-full touch-none bg-white"
        />
        <div
          v-if="bounds"
          class="pointer-events-none absolute border-2 border-primary bg-primary/20"
          :style="overlay"
        />
      </div>
    </div>
  </div>
</template>
