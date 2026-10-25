<script setup lang="ts">
import { pengoloMutation } from "~/utils/pengolo-api";
import { ArrowDownTrayIcon } from "@heroicons/vue/24/outline";
const props = defineProps<{
  file: { id: string; name?: string; mime?: string; size?: number } | null;
  temporarySession?: string;
}>();
const emit = defineEmits<{ close: [] }>();
const { t } = useI18n();
const account = useAccount();
const loading = ref(false),
  error = ref(""),
  url = ref(""),
  mime = ref(""),
  resolvedName = ref("");
let epoch = 0;
let controller: AbortController | undefined;
function clear() {
  epoch++;
  controller?.abort();
  controller = undefined;
  if (url.value.startsWith("blob:")) URL.revokeObjectURL(url.value);
  url.value = "";
  loading.value = false;
  error.value = "";
  resolvedName.value = "";
}
const title = computed(
  () => props.file?.name || resolvedName.value || t("pengolo.documentSource"),
);
async function load() {
  clear();
  const file = props.file;
  if (!file) return;
  const own = epoch;
  controller = new AbortController();
  const signal = controller.signal;
  loading.value = true;
  try {
    if (file.size !== undefined && file.size > 50 * 1024 * 1024)
      throw new Error("PREVIEW_LIMIT");
    const result = await pengoloMutation<{
      url: string;
      name?: string | null;
      mime?: string | null;
      size?: number | null;
    }>(
      "pengoloDownload",
      {
        id: file.id,
        temporarySession: props.temporarySession,
      },
      signal,
    );
    if (own !== epoch) return;
    resolvedName.value = result.name ?? "";
    if (
      result.size !== undefined &&
      result.size !== null &&
      result.size > 50 * 1024 * 1024
    )
      throw new Error("PREVIEW_LIMIT");
    const target = new URL(result.url, window.location.origin);
    if (target.origin !== window.location.origin)
      throw new Error("PREVIEW_ORIGIN");
    const response = await fetch(target, {
      method: "HEAD",
      credentials: "same-origin",
      redirect: "error",
      signal,
    });
    if (!response.ok) throw new Error("PREVIEW_UNAVAILABLE");
    mime.value =
      (
        file.mime ||
        result.mime ||
        response.headers.get("content-type") ||
        "application/octet-stream"
      ).split(";")[0] ?? "";
    if (
      mime.value !== "application/pdf" &&
      ![
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "text/plain",
        "text/csv",
      ].includes(mime.value)
    )
      throw new Error("PREVIEW_UNSUPPORTED");
    target.searchParams.set("preview", "true");
    const contents = await fetch(target, {
      credentials: "same-origin",
      redirect: "error",
      signal,
    });
    if (!contents.ok || !contents.body) throw new Error("PREVIEW_UNAVAILABLE");
    const reader = contents.body.getReader(),
      parts: Uint8Array<ArrayBuffer>[] = [];
    let size = 0;
    try {
      while (true) {
        const part = await reader.read();
        if (part.done) break;
        size += part.value.byteLength;
        if (size > 50 * 1024 * 1024) {
          await reader.cancel();
          throw new Error("PREVIEW_LIMIT");
        }
        parts.push(part.value);
      }
    } finally {
      reader.releaseLock();
    }
    if (own !== epoch) return;
    const blob = new Blob(parts, { type: mime.value });
    url.value = URL.createObjectURL(blob);
  } catch (failure) {
    if (
      own === epoch &&
      !(failure instanceof DOMException && failure.name === "AbortError")
    )
      error.value = t("documents.previewUnavailable");
  } finally {
    if (own === epoch) loading.value = false;
  }
}
async function download() {
  const file = props.file;
  if (!file) return;
  const own = epoch;
  try {
    const result = await pengoloMutation<{ url: string }>(
      "pengoloDownload",
      { id: file.id, temporarySession: props.temporarySession },
      controller?.signal,
    );
    if (own !== epoch) return;
    const target = new URL(result.url, window.location.origin);
    if (target.origin !== window.location.origin)
      throw new Error("PREVIEW_ORIGIN");
    target.searchParams.delete("preview");
    const link = document.createElement("a");
    link.href = target.href;
    link.download = file.name || resolvedName.value || "document";
    link.click();
  } catch {
    if (own === epoch) error.value = t("documents.previewUnavailable");
  }
}
watch(() => props.file?.id, load, { immediate: true });
watch(
  () => [account.session.value?.subject, account.session.value?.tenant],
  () => {
    clear();
    emit("close");
  },
);
onDeactivated(clear);
onBeforeUnmount(clear);
</script>
<template>
  <UiDialog :open="!!file" :title="title" size="wide" @close="emit('close')">
    <p v-if="loading" role="status">{{ $t("documents.previewLoading") }}</p>
    <UiAlert v-if="error" tone="danger"
      >{{ error }}
      <UiButton variant="ghost" :action="load">{{
        $t("email.retry")
      }}</UiButton></UiAlert
    >
    <img
      v-if="url && mime.startsWith('image/')"
      :src="url"
      :alt="title"
      class="mx-auto max-h-[75vh] max-w-full"
    />
    <iframe
      v-else-if="url"
      :src="url"
      :title="title"
      :sandbox="mime === 'application/pdf' ? undefined : ''"
      class="h-[75vh] w-full border-0"
    />
    <template #footer>
      <UiButton
        variant="secondary"
        :disabled="loading || !url"
        :action="download"
        ><ArrowDownTrayIcon aria-hidden="true" />{{
          $t("documents.download")
        }}</UiButton
      >
    </template>
  </UiDialog>
</template>
