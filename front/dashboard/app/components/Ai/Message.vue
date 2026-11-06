<script setup lang="ts">
import DOMPurify from "dompurify";
import { ClipboardDocumentIcon, CheckIcon } from "@heroicons/vue/24/outline";
import { parseActivity, type EmailProposal } from "~/utils/pengolo-activity";
import {
  renderPengoloMarkdown,
  markdownWithTaskChecks,
  type ToolCheckpoint,
} from "~/utils/pengolo-markdown";
import {
  tableClipboard,
  tableWorkbook,
  downloadBlob,
} from "~/utils/pengolo-export";
import { usePengoloChats } from "~/stores/usePengoloChats";
import { usePengoloRuns } from "~/stores/usePengoloRuns";
import { usePengoloUi } from "~/stores/usePengoloUi";
import type { PengoloMessage, QuestionResponse } from "~/utils/pengolo-api";
const props = defineProps<{
  message: PengoloMessage;
  streaming?: boolean;
  anchorId?: string;
  toolStatus?: string;
}>();
const { t } = useI18n();
const emailProposals = computed<EmailProposal[]>(() =>
  (props.message.emailDrafts ?? []).flatMap((content) => {
    const parsed = parseActivity(content);
    return parsed?.type === "email_draft" ? [parsed] : [];
  }),
);
const chats = usePengoloChats();
const runs = usePengoloRuns();
const ui = usePengoloUi();
const activityRun = computed(() => {
  const id = props.message.generationId;
  if (props.message.role !== "assistant" || !id) return;
  const run = runs.runs[id];
  if (
    run &&
    run.chatId === chats.active?.id &&
    run.temporary === ui.temporary &&
    (props.streaming || run.events.some((event) => event.kind === "activity"))
  )
    return run;
});
async function answerQuestion(answer: QuestionResponse) {
  const run = activityRun.value;
  if (!run) throw new Error("Question unavailable");
  await runs.answer(run.id, answer);
}
const account = useState<{ subject: string } | null>("account-session");
const error = ref("");
const emailReference = ref("");
const editable = computed(
  () =>
    !props.streaming &&
    chats.active?.ownerId === account.value?.subject &&
    ["completed", "failed", "cancelled"].includes(
      props.message.status ?? "completed",
    ),
);
const text = computed(() =>
  markdownWithTaskChecks(
    props.message.parts
      ?.flatMap((part) => (part.kind === "markdown" ? [part.text] : []))
      .join("\n\n") || props.message.text,
    Object.fromEntries(
      (props.message.taskChecks ?? []).map((check) => [
        String(check.index),
        check.checked,
      ]),
    ),
  ),
);
const checkpoints = computed<ToolCheckpoint[]>(() => {
  if (props.message.toolCheckpoints?.length)
    return props.message.toolCheckpoints;
  if (!activityRun.value) return [];
  const result: ToolCheckpoint[] = [];
  let offset = 0;
  for (const event of activityRun.value.events) {
    if (event.kind === "content") offset += event.content.length;
    if (event.kind === "tool")
      result.push({ offset, operation: event.content });
    if (result.length === 32) break;
  }
  return result;
});
function checkpointLabel(operation: string) {
  const labels: Record<string, string> = {
    web: "web",
    library: "documents",
    read_file: "documents",
    document_search: "documents",
    date_interval: "calculation",
    run_python: "calculation",
    consult_specialist: "consultation",
  };
  return t(`pengolo.analysis.checkpoint.${labels[operation] ?? "tool"}`);
}
const rendered = computed(() => {
  const result = renderPengoloMarkdown(
    text.value,
    props.message.id,
    editable.value,
    t("pengolo.scrollableTable"),
    checkpoints.value,
    props.streaming,
  );
  return {
    ...result,
    blocks: result.blocks.map((block) =>
      block.kind === "markdown"
        ? {
            ...block,
            html: DOMPurify.sanitize(block.html, {
              ADD_ATTR: ["target", "referrerpolicy"],
            }),
          }
        : block,
    ),
  };
});
const references = computed(() =>
  (props.message.evidence ?? []).filter(
    (reference) =>
      reference.resourceType !== "pengolo_file" ||
      !props.message.fileIds?.includes(reference.resourceId),
  ),
);
const artifacts = computed(
  () => props.message.parts?.filter((part) => part.kind === "artifact") ?? [],
);
const copied = ref(false);
let copyTimer: ReturnType<typeof setTimeout> | undefined;
let copyVersion = 0;
function resetCopy() {
  copyVersion++;
  clearTimeout(copyTimer);
  copied.value = false;
}
onDeactivated(resetCopy);
onBeforeUnmount(resetCopy);
async function copy() {
  resetCopy();
  const version = copyVersion;
  try {
    await navigator.clipboard.writeText(text.value);
    if (version !== copyVersion) return;
    copied.value = true;
    error.value = "";
    copyTimer = setTimeout(resetCopy, 650);
  } catch {
    if (version === copyVersion) error.value = t("pengolo.copyResponseError");
  }
}
async function action(event: MouseEvent) {
  const target = event.target;
  if (!(target instanceof Element)) return;
  const reference = target.closest<HTMLAnchorElement>(
    "a[data-email-reference]",
  );
  if (
    reference &&
    /^GDR-[A-F0-9]{32}$/.test(reference.dataset.emailReference ?? "")
  ) {
    event.preventDefault();
    emailReference.value = reference.dataset.emailReference ?? "";
    return;
  }
  const button = target.closest<HTMLButtonElement>(
    "button[data-code-copy],button[data-table-copy],button[data-table-download]",
  );
  if (!button) return;
  try {
    error.value = "";
    if (button.dataset.codeCopy !== undefined)
      await navigator.clipboard.writeText(
        rendered.value.codes[Number(button.dataset.codeCopy)] ?? "",
      );
    else {
      const rows =
        rendered.value.tables[
          Number(button.dataset.tableCopy ?? button.dataset.tableDownload)
        ];
      if (!rows) return;
      if (button.dataset.tableCopy !== undefined)
        await navigator.clipboard.writeText(tableClipboard(rows));
      else
        downloadBlob(
          new Blob([await tableWorkbook(rows)], {
            type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          }),
          "pengolo-table.xlsx",
        );
    }
  } catch {
    error.value = "This content could not be copied or exported.";
  }
}
async function checkTask(event: Event) {
  const target = event.target;
  const chat = chats.active;
  if (
    !(target instanceof HTMLInputElement) ||
    target.dataset.taskIndex === undefined ||
    !chat ||
    !editable.value
  )
    return;
  const checked = target.checked;
  target.disabled = true;
  const index = Number(target.dataset.taskIndex);
  const checks = new Map(
    (props.message.taskChecks ?? []).map((check) => [
      check.index,
      check.checked,
    ]),
  );
  checks.set(index, checked);
  try {
    error.value = "";
    await chats.update(chat, {
      messageId: props.message.id,
      taskChecks: [...checks].map(([index, checked]) => ({ index, checked })),
      temporary: ui.temporary,
    });
    const message = chats.active?.data.messages?.find(
      (message) => message.id === props.message.id,
    );
    if (message)
      message.taskChecks = [...checks].map(([index, checked]) => ({
        index,
        checked,
      }));
  } catch {
    target.checked = !checked;
    error.value = "This task could not be saved. Please retry.";
  } finally {
    target.disabled = !editable.value;
  }
}
</script>

<template>
  <article
    :id="anchorId"
    :tabindex="anchorId ? -1 : undefined"
    :class="['p-message', message.role]"
    :aria-label="message.role === 'user' ? 'Your message' : 'Pengolo response'"
  >
    <div class="p-message-body">
      <div v-if="message.role === 'user'" class="p-user-text">
        {{ message.text }}
      </div>
      <div
        v-else-if="text || checkpoints.length"
        class="p-markdown"
        @click="action"
        @change="checkTask"
      >
        <template v-for="(block, index) in rendered.blocks" :key="index">
          <div v-if="block.kind === 'markdown'" v-html="block.html" />
          <AiChart v-else-if="block.kind === 'chart'" :chart="block.chart" />
          <ul
            v-else-if="block.kind === 'checkpoint'"
            class="p-inline-checkpoint"
            :aria-label="$t('pengolo.analysis.checks')"
          >
            <li v-for="operation in block.operations" :key="operation">
              {{ checkpointLabel(operation) }}
            </li>
          </ul>
          <AiEvidenceBlock v-else :block="block" />
        </template>
      </div>
      <p v-if="message.status === 'failed'" class="p-error" role="alert">
        Generation failed. Your message has been kept. You can retry.
      </p>
      <p v-if="error" class="p-error" role="alert">{{ error }}</p>
      <AiRunActivity
        v-if="activityRun"
        :events="activityRun.events"
        :status="activityRun.status"
        :can-answer="streaming === true && activityRun.canAnswer"
        :submit="answerQuestion"
        :fallback-status="toolStatus"
      />
      <AiExecutionHistory
        :has-calculations="message.activityKinds?.includes('python')"
        :inline-questions="message.activityKinds?.includes('question')"
        :has-other-activity="
          message.activityKinds?.some((kind) => kind !== 'question')
        "
        v-else-if="
          message.role === 'assistant' &&
          message.generationId &&
          !streaming &&
          !!message.activityKinds?.length
        "
        :generation-id="message.generationId"
        :temporary="ui.temporary"
      />
      <div v-if="message.fileIds?.length" class="p-file-chips">
        <AiDocumentAttachment
          v-for="id in message.fileIds"
          :key="id"
          :id="id"
          :temporary-session="ui.temporary ? chats.active?.id : undefined"
        />
      </div>
      <div
        v-if="references.length"
        class="p-file-chips"
        aria-label="Evidence references"
      >
        <template
          v-for="reference in references"
          :key="`${reference.resourceType}/${reference.resourceId}`"
        >
          <AiDocumentAttachment
            v-if="reference.resourceType === 'pengolo_file'"
            :id="reference.resourceId"
            :referenced-text="message.role === 'assistant' ? text : undefined"
            :temporary-session="ui.temporary ? chats.active?.id : undefined"
          />
          <span v-else class="p-chip">{{ $t("pengolo.evidenceSource") }}</span>
        </template>
      </div>
      <div v-for="artifact in artifacts" :key="artifact.id" class="p-chip">
        {{ artifact.title }}
      </div>
      <AiEmailProposal
        v-for="proposal in emailProposals"
        :key="proposal.id"
        :proposal="proposal"
      />
      <UiIconButton
        v-if="
          message.role === 'assistant' &&
          message.status !== 'failed' &&
          message.text &&
          !streaming
        "
        :class="{ 'p-copy-confirmed': copied }"
        :icon="copied ? CheckIcon : ClipboardDocumentIcon"
        :label="
          copied ? $t('pengolo.copiedResponse') : $t('pengolo.copyResponse')
        "
        :action="copy"
      />
    </div>
    <LazyEmailReference
      v-if="emailReference"
      :reference="emailReference"
      @close="emailReference = ''"
    />
  </article>
</template>
