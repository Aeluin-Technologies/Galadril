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
import { usePengoloModels } from "~/stores/usePengoloModels";
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
async function recover() {
  const chat = chats.active;
  if (!chat || !props.message.generationId) return;
  try {
    error.value = "";
    await runs.send({
      idempotencyKey: crypto.randomUUID(),
      chatId: chat.id,
      projectId: chat.projectId,
      prompt: t("pengolo.execution.continue"),
      modelAlias:
        chat.data.generationContext?.modelAlias || usePengoloModels().selected,
      resumeSource: props.message.generationId,
      executionMode: ui.executionMode,
      temporary: ui.temporary,
      withLibrary: ui.withLibrary,
    });
  } catch {
    error.value = t("pengolo.execution.recoveryUnavailable");
  }
}
async function answerQuestion(answer: QuestionResponse) {
  const run = activityRun.value;
  if (!run) throw new Error("Question unavailable");
  await runs.answer(run.id, answer);
}
const account = useState<{ subject: string } | null>("account-session");
const error = ref("");
const emailReference = ref("");
const specialistReport = ref("");
watch(
  () => props.message.id,
  () => {
    specialistReport.value = "";
  },
);
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
const documentPreview = shallowRef<{ id: string; name: string } | null>(null);
let copyTimer: ReturnType<typeof setTimeout> | undefined;
let copyVersion = 0;
function resetCopy() {
  copyVersion++;
  clearTimeout(copyTimer);
  copied.value = false;
}
const feedbackTimers = new Map<
  HTMLButtonElement,
  ReturnType<typeof setTimeout>
>();
const actionNotice = ref("");
let actionVersion = 0;
function resetFeedback() {
  documentPreview.value = null;
  resetCopy();
  actionVersion++;
  for (const [button, timer] of feedbackTimers) {
    clearTimeout(timer);
    button.classList.remove("p-action-confirmed");
  }
  feedbackTimers.clear();
  actionNotice.value = "";
}
onDeactivated(resetFeedback);
onBeforeUnmount(resetFeedback);
async function copy() {
  resetCopy();
  const version = copyVersion;
  try {
    await navigator.clipboard.writeText(
      props.message.role === "user" ? props.message.text : text.value,
    );
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
  const fileLink = target.closest<HTMLAnchorElement>("a[href]");
  const fileId = fileLink?.pathname.match(
    /^\/pengolo\/files\/([a-f0-9]{32})$/,
  )?.[1];
  if (fileLink?.origin === window.location.origin && fileId) {
    event.preventDefault();
    documentPreview.value = {
      id: fileId,
      name: fileLink.textContent?.trim() || t("pengolo.documentSource"),
    };
    return;
  }
  const reportLink = target.closest<HTMLAnchorElement>(
    "a[data-specialist-report]",
  );
  if (
    reportLink &&
    /^[a-f0-9]{64}$/.test(reportLink.dataset.specialistReport ?? "") &&
    props.message.generationId
  ) {
    event.preventDefault();
    specialistReport.value = reportLink.dataset.specialistReport ?? "";
    return;
  }
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
  if (!button || button.disabled) return;
  const version = actionVersion;
  button.disabled = true;
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
    if (version === actionVersion && button.isConnected) {
      clearTimeout(feedbackTimers.get(button));
      button.setAttribute("aria-label", button.textContent ?? "");
      button.classList.add("p-action-confirmed");
      actionNotice.value = t("pengolo.actionCompleted");
      feedbackTimers.set(
        button,
        setTimeout(() => {
          button.classList.remove("p-action-confirmed");
          feedbackTimers.delete(button);
          actionNotice.value = "";
        }, 650),
      );
    }
  } catch {
    if (version === actionVersion)
      error.value = "This content could not be copied or exported.";
  } finally {
    button.disabled = false;
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
      <p v-if="message.sourceUnavailable" class="text-muted" role="status">
        {{ $t("pengolo.sourceUnavailable") }}
      </p>
      <AiRunActivity
        v-if="activityRun && !streaming"
        :operations="[
          ...new Set(
            checkpoints.map((item) => checkpointLabel(item.operation)),
          ),
        ]"
        :events="activityRun.events"
        :status="activityRun.status"
        :can-answer="false"
        :submit="answerQuestion"
        :fallback-status="toolStatus"
      />
      <AiExecutionHistory
        :has-web-research="message.activityKinds?.includes('web_research')"
        :operations="[
          ...new Set(
            checkpoints.map((item) => checkpointLabel(item.operation)),
          ),
        ]"
        :has-calculations="message.activityKinds?.includes('python')"
        :inline-questions="message.activityKinds?.includes('question')"
        :has-other-activity="
          !!checkpoints.length ||
          message.activityKinds?.some((kind) => kind !== 'question')
        "
        v-else-if="
          message.role === 'assistant' &&
          message.generationId &&
          !streaming &&
          (!!message.activityKinds?.length || !!checkpoints.length)
        "
        :generation-id="message.generationId"
        :temporary="ui.temporary"
      />
      <details
        v-else-if="
          !streaming && message.role === 'assistant' && checkpoints.length
        "
        class="p-thinking"
      >
        <summary>{{ $t("pengolo.analysis.reasoning") }}</summary>
        <ul>
          <li
            v-for="operation in [
              ...new Set(checkpoints.map((item) => item.operation)),
            ]"
            :key="operation"
          >
            {{ checkpointLabel(operation) }}
          </li>
        </ul>
      </details>

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
          <template v-else-if="block.kind === 'checkpoint'">
            <details v-if="streaming" class="p-thinking">
              <summary>{{ $t("pengolo.analysis.thinking") }}</summary>
              <ul :aria-label="$t('pengolo.analysis.checks')">
                <li v-for="operation in block.operations" :key="operation">
                  <span>{{ checkpointLabel(operation) }}</span>
                </li>
              </ul>
            </details>
          </template>
          <AiEvidenceBlock v-else :block="block" />
        </template>
      </div>
      <p v-if="message.status === 'failed'" class="p-error" role="alert">
        {{ $t("pengolo.execution.interrupted") }}
      </p>
      <UiButton
        v-if="
          message.role === 'assistant' &&
          message.status === 'failed' &&
          message.generationId &&
          editable
        "
        variant="ghost"
        :action="recover"
        :disabled="runs.sending || !!chats.active?.data.activeGeneration"
        >{{ $t("pengolo.execution.continue") }}</UiButton
      >
      <p v-if="error" class="p-error" role="alert">{{ error }}</p>
      <AiRunActivity
        v-if="activityRun && streaming"
        :events="activityRun.events"
        :status="activityRun.status"
        :can-answer="streaming === true && activityRun.canAnswer"
        :submit="answerQuestion"
        :fallback-status="toolStatus"
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
      <span class="sr-only" role="status" aria-live="polite">{{
        actionNotice
      }}</span>
    </div>
    <UiIconButton
      v-if="
        (message.role === 'user' || message.status !== 'failed') &&
        message.text &&
        !streaming
      "
      class="p-message-copy"
      :class="{ 'p-copy-confirmed': copied }"
      :icon="copied ? CheckIcon : ClipboardDocumentIcon"
      :label="
        copied
          ? $t('pengolo.copiedResponse')
          : message.role === 'user'
            ? $t('pengolo.copyMessage')
            : $t('pengolo.copyResponse')
      "
      :action="copy"
    />
    <LazyEmailReference
      v-if="emailReference"
      :reference="emailReference"
      @close="emailReference = ''"
    />
    <LazyAiReportPanel
      v-if="specialistReport && message.generationId"
      :report-id="specialistReport"
      :generation-id="message.generationId"
      :temporary="ui.temporary"
      @close="specialistReport = ''"
    />
    <LazyAiDocumentPreview
      v-if="documentPreview"
      :file="documentPreview"
      :temporary-session="ui.temporary ? chats.active?.id : undefined"
      @close="documentPreview = null"
    />
  </article>
</template>
