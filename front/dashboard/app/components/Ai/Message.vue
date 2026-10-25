<script setup lang="ts">
import DOMPurify from "dompurify";
import { parseActivity, type EmailProposal } from "~/utils/pengolo-activity";
import {
  renderPengoloMarkdown,
  markdownWithTaskChecks,
} from "~/utils/pengolo-markdown";
import {
  tableClipboard,
  tableWorkbook,
  downloadBlob,
} from "~/utils/pengolo-export";
import { usePengoloChats } from "~/stores/usePengoloChats";
import { usePengoloUi } from "~/stores/usePengoloUi";
import type { PengoloMessage } from "~/utils/pengolo-api";
const props = defineProps<{
  message: PengoloMessage;
  streaming?: boolean;
  anchorId?: string;
}>();
const { t } = useI18n();
const emailProposals = computed<EmailProposal[]>(() =>
  (props.message.emailDrafts ?? []).flatMap((content) => {
    const parsed = parseActivity(content);
    return parsed?.type === "email_draft" ? [parsed] : [];
  }),
);
const chats = usePengoloChats();
const ui = usePengoloUi();
const account = useState<{ subject: string } | null>("account-session");
const error = ref("");
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
const rendered = computed(() => {
  const result = renderPengoloMarkdown(
    text.value,
    props.message.id,
    editable.value,
    t("pengolo.scrollableTable"),
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
const references = computed(() => props.message.evidence ?? []);
const artifacts = computed(
  () => props.message.parts?.filter((part) => part.kind === "artifact") ?? [],
);
const copied = ref(false);
async function copy() {
  try {
    await navigator.clipboard.writeText(text.value);
    copied.value = true;
  } catch {
    copied.value = false;
    error.value = "The response could not be copied.";
  }
}
async function action(event: MouseEvent) {
  const target = event.target;
  if (!(target instanceof Element)) return;
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
      <div v-else class="p-markdown" @click="action" @change="checkTask">
        <template v-for="(block, index) in rendered.blocks" :key="index">
          <div v-if="block.kind === 'markdown'" v-html="block.html" />
          <AiChart v-else :chart="block.chart" />
        </template>
      </div>
      <p v-if="message.status === 'failed'" class="p-error" role="alert">
        Generation failed. Your message has been kept. You can retry.
      </p>
      <p v-if="error" class="p-error" role="alert">{{ error }}</p>
      <AiExecutionHistory
        v-if="
          message.role === 'assistant' && message.generationId && !streaming
        "
        :generation-id="message.generationId"
        :temporary="ui.temporary"
      />
      <div v-if="message.fileIds?.length" class="p-file-chips">
        <span v-for="id in message.fileIds" :key="id" class="p-chip"
          >Document · {{ id.slice(0, 8) }}</span
        >
      </div>
      <div
        v-if="references.length"
        class="p-file-chips"
        aria-label="Evidence references"
      >
        <span
          v-for="reference in references"
          :key="`${reference.resourceType}/${reference.resourceId}`"
          class="p-chip"
          >{{ reference.resourceType.replaceAll("_", " ") }} ·
          {{ reference.resourceId }}</span
        >
      </div>
      <div v-for="artifact in artifacts" :key="artifact.id" class="p-chip">
        {{ artifact.title }}
      </div>
      <p v-if="streaming && !message.text" class="p-muted" role="status">
        Working with your authorized context<span class="p-thinking">…</span>
      </p>
      <UiButton
        variant="ghost"
        v-if="message.role === 'assistant' && message.text && !streaming"
        type="button"
        @click="copy"
      >
        {{ copied ? "Copied" : "Copy response" }}
      </UiButton>
      <AiEmailProposal
        v-for="proposal in emailProposals"
        :key="proposal.id"
        :proposal="proposal"
      />
    </div>
  </article>
</template>
