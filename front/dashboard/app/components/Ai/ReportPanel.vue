<script setup lang="ts">
import DOMPurify from "dompurify";
import { useEventListener } from "@vueuse/core";
import { pengoloRequest } from "~/utils/pengolo-api";
import { renderPengoloMarkdown } from "~/utils/pengolo-markdown";
import { usePengoloAgents } from "~/stores/usePengoloAgents";
const props = defineProps<{
  reportId: string;
  generationId: string;
  temporary: boolean;
}>();
const emit = defineEmits<{ close: [] }>();
const { t } = useI18n();
const agents = usePengoloAgents();
const account = useAccount();
interface Report {
  id: string;
  agentId: string;
  content: string;
}
const report = ref<Report | null>(null);
const loading = ref(false);
const error = ref("");
let controller: AbortController | undefined;
const title = computed(
  () =>
    agents.items.find((agent) => agent.id === report.value?.agentId)?.data
      .name || t("pengolo.report.title"),
);
const blocks = computed(() =>
  renderPengoloMarkdown(
    report.value?.content ?? "",
    "specialist-report",
    false,
    t("pengolo.scrollableTable"),
  ).blocks.map((block) =>
    block.kind === "markdown"
      ? {
          ...block,
          html: DOMPurify.sanitize(block.html, {
            ADD_ATTR: ["target", "referrerpolicy"],
          }),
        }
      : block,
  ),
);
function clear() {
  controller?.abort();
  controller = undefined;
  report.value = null;
  loading.value = false;
  error.value = "";
}
function close() {
  clear();
  emit("close");
}
async function load() {
  clear();
  if (!/^[a-f0-9]{64}$/.test(props.reportId) || !props.generationId) return;
  const request = new AbortController();
  controller = request;
  loading.value = true;
  try {
    const result = await pengoloRequest<{ specialistReport: Report | null }>(
      "query SpecialistReport($generationId: String!, $reportId: String!, $temporary: Boolean!) { pengoloEvents(generationId: $generationId, after: 0, temporary: $temporary, reportId: $reportId) { specialistReport { id agentId content } } }",
      {
        generationId: props.generationId,
        reportId: props.reportId,
        temporary: props.temporary,
      },
      request.signal,
    );
    if (request.signal.aborted) return;
    if (
      !result.specialistReport ||
      result.specialistReport.id !== props.reportId
    )
      throw new Error("Report unavailable");
    report.value = result.specialistReport;
  } catch {
    if (!request.signal.aborted) error.value = t("pengolo.report.unavailable");
  } finally {
    if (!request.signal.aborted) loading.value = false;
  }
}
watch(() => [props.reportId, props.generationId, props.temporary], load, {
  immediate: true,
});
watch(
  () => [account.session.value?.subject, account.session.value?.tenant],
  close,
);
useEventListener("pengolo-access-invalidated", close);
onDeactivated(close);
onBeforeUnmount(clear);
</script>
<template>
  <UiDialog
    :open="!!reportId"
    :title="title"
    :close-label="$t('pengolo.report.close')"
    placement="right"
    :modal="false"
    @close="close"
  >
    <p v-if="loading" role="status">{{ $t("pengolo.report.loading") }}</p>
    <UiAlert v-else-if="error" tone="danger">{{ error }}</UiAlert>
    <div v-else class="p-markdown">
      <template v-for="(block, index) in blocks" :key="index">
        <div v-if="block.kind === 'markdown'" v-html="block.html" />
        <AiChart v-else-if="block.kind === 'chart'" :chart="block.chart" />
        <AiEvidenceBlock
          v-else-if="block.kind !== 'checkpoint'"
          :block="block"
        />
      </template>
    </div>
  </UiDialog>
</template>
