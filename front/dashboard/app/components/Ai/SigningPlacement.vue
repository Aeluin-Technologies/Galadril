<script setup lang="ts">
import {
  getDocument,
  GlobalWorkerOptions,
  type PDFDocumentProxy,
} from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import type { SigningPlacement } from "~/utils/pdf-signing";

const props = defineProps<{
  bytes: Uint8Array<ArrayBuffer>;
  editable: boolean;
}>();
const placement = defineModel<SigningPlacement>();
const pdf = shallowRef<PDFDocumentProxy>();
const scroller = useTemplateRef<HTMLDivElement>("scroller");
const sentinel = useTemplateRef<HTMLDivElement>("sentinel");
const error = ref(false);
const batchSize = 12;
const pages = ref(0);
let observer: IntersectionObserver | undefined;
let disposed = false;
GlobalWorkerOptions.workerSrc = workerUrl;
const task = getDocument({ data: props.bytes.slice(), enableXfa: false });

onMounted(async () => {
  try {
    const document = await task.promise;
    if (disposed) return;
    const initial = placement.value?.page ?? 0;
    pages.value = Math.min(document.numPages, Math.max(batchSize, initial + 1));
    pdf.value = document;
    await nextTick();
    if (disposed || !scroller.value || !sentinel.value) return;
    scroller.value
      .querySelector(`[data-signing-page="${initial}"]`)
      ?.scrollIntoView({ block: "nearest" });
    observer = new IntersectionObserver(
      ([entry]) => {
        if (entry?.isIntersecting)
          pages.value = Math.min(document.numPages, pages.value + batchSize);
      },
      { root: scroller.value, rootMargin: "400px" },
    );
    observer.observe(sentinel.value);
  } catch {
    if (!disposed) error.value = true;
  }
});
onBeforeUnmount(() => {
  disposed = true;
  observer?.disconnect();
  void task.destroy().catch(() => {});
});
</script>

<template>
  <UiAlert v-if="error" tone="danger">{{
    $t("documentSigning.unavailable")
  }}</UiAlert>
  <p v-else-if="!pdf" role="status">{{ $t("documentSigning.loading") }}</p>
  <div
    v-else
    ref="scroller"
    :aria-label="$t('documentSigning.placement')"
    class="max-h-[58dvh] overflow-auto rounded-panel border border-border bg-surface-muted p-2 sm:p-4"
  >
    <div class="mx-auto grid max-w-2xl gap-3">
      <AiSigningPlacementPage
        v-for="index in pages"
        :key="index"
        v-model="placement"
        :pdf="pdf"
        :page="index - 1"
        :editable="editable"
      />
      <div ref="sentinel" aria-hidden="true" />
    </div>
  </div>
</template>
