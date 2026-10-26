<script setup lang="ts">
import { DocumentTextIcon } from "@heroicons/vue/24/outline";
import { pengoloMutation } from "~/utils/pengolo-api";
import { documentMentioned } from "~/utils/pengolo";

const props = defineProps<{
  id: string;
  temporarySession?: string;
  referencedText?: string;
}>();
const account = useAccount();
const file = shallowRef<{
  id: string;
  name: string;
  mime?: string;
  size?: number;
} | null>(null);
const cited = computed(
  () =>
    props.referencedText === undefined ||
    (!!file.value &&
      documentMentioned(props.referencedText, props.id, file.value.name)),
);
const loading = ref(false),
  opened = ref(false);
let epoch = 0,
  active = true;
let controller: AbortController | undefined;
function clear() {
  epoch++;
  controller?.abort();
  controller = undefined;
  file.value = null;
  opened.value = false;
  loading.value = false;
}
async function load() {
  clear();
  if (!active || !account.session.value) return;
  const own = epoch;
  controller = new AbortController();
  loading.value = true;
  try {
    const result = await pengoloMutation<{
      name?: string | null;
      mime?: string | null;
      size?: number | null;
    }>(
      "pengoloDownload",
      { id: props.id, temporarySession: props.temporarySession },
      controller.signal,
    );
    if (own === epoch && result.name)
      file.value = {
        id: props.id,
        name: result.name,
        mime: result.mime ?? undefined,
        size: result.size ?? undefined,
      };
  } catch {
    if (own === epoch) file.value = null;
  } finally {
    if (own === epoch) loading.value = false;
  }
}
watch(
  () => [
    props.id,
    props.temporarySession,
    account.session.value?.subject,
    account.session.value?.tenant,
  ],
  load,
  { immediate: true },
);
onDeactivated(() => {
  active = false;
  clear();
});
onActivated(() => {
  active = true;
  if (!file.value && !loading.value) void load();
});
onBeforeUnmount(clear);
</script>
<template>
  <UiButton
    v-if="cited"
    variant="secondary"
    size="sm"
    :disabled="!file"
    :loading="loading"
    :tooltip="file?.name ?? $t('documents.attachmentUnavailable')"
    class="max-w-full"
    @click="opened = true"
  >
    <DocumentTextIcon aria-hidden="true" />
    <span class="min-w-0 max-w-56 truncate">{{
      file?.name ??
      $t(
        loading
          ? "documents.attachmentLoading"
          : "documents.attachmentUnavailable",
      )
    }}</span>
  </UiButton>
  <AiDocumentPreview
    :file="opened ? file : null"
    :temporary-session="temporarySession"
    @close="opened = false"
  />
</template>
