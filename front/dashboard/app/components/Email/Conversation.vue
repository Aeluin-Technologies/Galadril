<script setup lang="ts">
import { EllipsisHorizontalIcon } from "@heroicons/vue/24/outline";
import { parseEmailThread } from "~/utils/email-thread";
import type { EmailThread } from "~/utils/email-thread";
import { object, parseMessages, text, number } from "~/utils/email";
import type { EmailMessage } from "~/utils/email";

const props = defineProps<{ account: string; messageId: string }>();
const api = useEmailApi();
const identity = useAccount();
const { t } = useI18n();
const regionId = useId();
const open = ref(false),
  loading = ref(false),
  failed = ref(false);
const thread = shallowRef<EmailThread>();
const selected = shallowRef<EmailMessage>();
const attachmentText = ref(""),
  nextOffset = ref(0),
  attachmentIndex = ref(-1);
let epoch = 0,
  active = true;

function clear() {
  epoch++;
  api.cancel();
  open.value = false;
  loading.value = false;
  thread.value = undefined;
  selected.value = undefined;
  attachmentText.value = "";
  nextOffset.value = 0;
  attachmentIndex.value = -1;
  failed.value = false;
}
async function load() {
  const own = ++epoch;
  loading.value = true;
  failed.value = false;
  try {
    const result = parseEmailThread(
      await api.operation({
        operation: "thread",
        account: props.account,
        message: props.messageId,
      }),
    );
    if (own === epoch && active) thread.value = result;
  } catch {
    if (own === epoch && active) failed.value = true;
  } finally {
    if (own === epoch && active) loading.value = false;
  }
}
async function toggle() {
  if (open.value) {
    clear();
    return;
  }
  open.value = true;
  await load();
}
async function read(id: string) {
  const own = ++epoch;
  api.cancel();
  selected.value = undefined;
  attachmentText.value = "";
  nextOffset.value = 0;
  attachmentIndex.value = -1;
  loading.value = true;
  failed.value = false;
  try {
    const message = await api.operation({
      operation: "message",
      account: props.account,
      message: id,
    });
    if (own === epoch && active)
      selected.value = parseMessages([
        { id, sequence: 0, label: "", folder: "", message },
      ])[0];
  } catch {
    if (own === epoch && active) failed.value = true;
  } finally {
    if (own === epoch && active) loading.value = false;
  }
}
async function readAttachment(index: number, append = false) {
  const source = selected.value;
  if (!source) return;
  const own = ++epoch;
  api.cancel();
  loading.value = true;
  failed.value = false;
  if (!append) attachmentText.value = "";
  try {
    const result = object(
      await api.operation({
        operation: "attachment",
        account: props.account,
        message: source.id,
        attachment: index,
        offset: append ? nextOffset.value : 0,
      }),
    );
    if (own !== epoch || !active) return;
    attachmentIndex.value = index;
    attachmentText.value =
      (append ? attachmentText.value : "") + text(result.text);
    nextOffset.value = number(result.next_offset);
    if (!attachmentText.value)
      attachmentText.value = t("email.chat.noAttachmentText");
  } catch {
    if (own === epoch && active) failed.value = true;
  } finally {
    if (own === epoch && active) loading.value = false;
  }
}
watch(
  () => [
    props.account,
    props.messageId,
    identity.session.value?.tenant,
    identity.session.value?.subject,
  ],
  clear,
);
onDeactivated(() => {
  active = false;
  clear();
});
onActivated(() => {
  active = true;
});
onBeforeUnmount(clear);
</script>

<template>
  <div>
    <UiButton
      variant="ghost"
      :aria-expanded="open"
      :aria-controls="regionId"
      @click="toggle"
    >
      <EllipsisHorizontalIcon aria-hidden="true" />{{
        $t(open ? "email.chat.hideThread" : "email.chat.showThread")
      }}
    </UiButton>
    <section
      v-if="open"
      :id="regionId"
      :aria-label="$t('email.chat.showThread')"
      class="mt-2 space-y-3"
    >
      <p v-if="thread" class="text-sm text-muted">
        {{ $t("email.chat.collectedThread") }}
      </p>
      <p v-if="loading" role="status" class="text-sm text-muted">
        {{ $t("email.chat.loadingThread") }}
      </p>
      <UiAlert v-if="failed" tone="danger"
        >{{ $t("email.loadError") }}
        <UiButton variant="ghost" :action="load">{{
          $t("email.chat.retryThread")
        }}</UiButton></UiAlert
      >
      <UiAlert
        v-if="thread?.truncated || (thread && !thread.indexed_complete)"
        tone="warning"
        >{{ $t("email.chat.partialThread") }}</UiAlert
      >
      <p v-if="thread && !thread.messages.length" class="text-sm text-muted">
        {{ $t("email.chat.emptyThread") }}
      </p>
      <ol v-if="thread" class="max-h-64 space-y-2 overflow-y-auto">
        <li v-for="message in thread.messages" :key="message.number">
          <UiButton
            v-if="message.received_id"
            variant="ghost"
            align="start"
            :disabled="loading"
            :aria-expanded="selected?.id === message.received_id"
            @click="read(message.received_id)"
          >
            {{ message.date }} — {{ message.subject }}
          </UiButton>
          <span v-else class="text-sm text-muted"
            >{{ message.date }} — {{ message.subject }} ({{
              message.status
            }})</span
          >
        </li>
      </ol>
      <article v-if="selected" class="space-y-2 border-t border-border pt-3">
        <p class="text-sm font-medium">
          {{ selected.message.sender }} — {{ selected.message.date }}
        </p>
        <p
          class="max-h-72 overflow-y-auto whitespace-pre-wrap break-words text-sm"
        >
          {{ selected.message.body }}
        </p>
        <div class="flex flex-wrap gap-2">
          <UiButton
            v-for="(attachment, index) in selected.message.attachments"
            :key="index"
            variant="ghost"
            :disabled="loading"
            @click="readAttachment(index)"
            >{{ attachment.name }}</UiButton
          >
        </div>
        <p
          v-if="attachmentText"
          class="max-h-72 overflow-y-auto whitespace-pre-wrap break-words text-sm"
        >
          {{ attachmentText }}
        </p>
        <UiButton
          v-if="nextOffset"
          variant="ghost"
          :disabled="loading"
          @click="readAttachment(attachmentIndex, true)"
          >{{ $t("email.chat.moreAttachment") }}</UiButton
        >
      </article>
    </section>
  </div>
</template>
