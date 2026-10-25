<script setup lang="ts">
import { ShareIcon } from "@heroicons/vue/24/outline";
import type {
  EmailAccount,
  EmailDraft,
  EmailMessage,
  EmailAttachment,
} from "~/utils/email";
import {
  can,
  parseMessages,
  parseMessagePage,
  parseDrafts,
  object,
  number,
} from "~/utils/email";
const props = defineProps<{ account: EmailAccount }>();
const api = useEmailApi();
const mailApi = useEmailApi();
const { t } = useI18n();
const messages = ref<EmailMessage[]>([]),
  drafts = ref<EmailDraft[]>([]),
  query = ref(""),
  selected = ref<EmailMessage>(),
  draft = ref<EmailDraft>(),
  reply = ref<EmailMessage>();
const nextBefore = ref(0),
  draftBefore = ref(0);
const searchLimited = ref(false);
const messageList = ref<HTMLElement>(),
  draftList = ref<HTMLElement>();
const composer = ref(false),
  reader = ref(false),
  threadMessage = ref(""),
  error = ref(""),
  success = ref(""),
  busy = ref(false),
  mailLoading = ref(false),
  draftLoading = ref(false);
const loading = computed(() => mailLoading.value || draftLoading.value);
let mailEpoch = 0,
  draftEpoch = 0,
  active = true;
let searchTimer: ReturnType<typeof setTimeout> | undefined;
async function loadMessages(append = false) {
  if (!active || (append && (mailLoading.value || !nextBefore.value))) return;
  const own = ++mailEpoch;
  mailLoading.value = true;
  try {
    const page = parseMessagePage(
      await mailApi.operation({
        operation: "messages",
        account: props.account.id,
        query: query.value,
        before: append ? nextBefore.value : 0,
      }),
    );
    if (own !== mailEpoch || !active) return;
    messages.value = append ? [...messages.value, ...page.items] : page.items;
    nextBefore.value = page.next_before;
    searchLimited.value = append
      ? searchLimited.value || !!page.approximate_limited
      : !!page.approximate_limited;
  } catch {
    if (own === mailEpoch && active) {
      nextBefore.value = 0;
      error.value = t("email.loadError");
    }
  } finally {
    if (own === mailEpoch) mailLoading.value = false;
  }
}
async function loadDrafts(append = false) {
  if (!active || (append && (draftLoading.value || !draftBefore.value))) return;
  const own = ++draftEpoch;
  draftLoading.value = true;
  try {
    const page = object(
      await api.operation({
        operation: "drafts_page",
        account: props.account.id,
        before: append ? draftBefore.value : 0,
      }),
    );
    if (own !== draftEpoch || !active) return;
    const items = parseDrafts(page.items);
    drafts.value = append ? [...drafts.value, ...items] : items;
    draftBefore.value = number(page.next_before);
  } catch {
    if (own === draftEpoch && active) {
      draftBefore.value = 0;
      error.value = t("email.loadError");
    }
  } finally {
    if (own === draftEpoch) draftLoading.value = false;
  }
}
async function load() {
  error.value = "";
  await Promise.all([loadMessages(), loadDrafts()]);
}
function moreMessages() {
  const element = messageList.value;
  if (
    element &&
    element.scrollHeight - element.clientHeight - element.scrollTop < 160
  )
    void loadMessages(true);
}
function moreDrafts() {
  const element = draftList.value;
  if (
    element &&
    element.scrollHeight - element.clientHeight - element.scrollTop < 160
  )
    void loadDrafts(true);
}
watch([nextBefore, mailLoading], () => nextTick(moreMessages));
watch([draftBefore, draftLoading], () => nextTick(moreDrafts));
watch(query, () => {
  mailEpoch++;
  mailApi.cancel();
  mailLoading.value = true;
  nextBefore.value = 0;
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    searchTimer = undefined;
    error.value = "";
    void loadMessages();
  }, 300);
});
function clear() {
  active = false;
  mailEpoch++;
  draftEpoch++;
  api.cancel();
  mailApi.cancel();
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = undefined;
  mailLoading.value = false;
  draftLoading.value = false;
  selected.value = undefined;
  messages.value = [];
  drafts.value = [];
  reader.value = false;
  threadMessage.value = "";
  composer.value = false;
}
watch(
  () => props.account.id,
  () => {
    clear();
    active = true;
    void load();
  },
  { immediate: true },
);
onDeactivated(clear);
onBeforeUnmount(clear);
onActivated(() => {
  if (!active) {
    active = true;
    void load();
  }
});
async function action(operation: string, value: EmailDraft | EmailMessage) {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  success.value = "";
  try {
    await api.operation({
      operation,
      account: props.account.id,
      ...("revision" in value
        ? { draft: value.id, revision: value.revision }
        : { message: value.id }),
    });
    await load();
    success.value = t("email.actionComplete");
  } catch {
    await load();
    error.value =
      operation === "send" ? t("email.sendError") : t("email.actionError");
  } finally {
    busy.value = false;
  }
}
async function edit(value?: EmailDraft, source?: EmailMessage) {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    draft.value = undefined;
    if (value) {
      const result = await api.operation({
        operation: "draft",
        account: props.account.id,
        draft: value.id,
      });
      draft.value = parseDrafts([result])[0];
      if (!draft.value) throw new Error("missing");
    }
    reply.value = source;
    composer.value = true;
  } catch {
    error.value = t("email.loadError");
  } finally {
    busy.value = false;
  }
}
async function read(value: EmailMessage | string) {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  const row =
    typeof value === "string"
      ? { id: value, sequence: 0, label: "", folder: "" }
      : value;
  threadMessage.value = "";
  try {
    const message = await api.operation({
      operation: "message",
      account: props.account.id,
      message: row.id,
    });
    selected.value = parseMessages([{ ...row, message }])[0];
    reader.value = !!selected.value;
  } catch {
    error.value = t("email.loadError");
  } finally {
    busy.value = false;
  }
}
function download(value: EmailAttachment) {
  const data = Uint8Array.from(atob(value.content), (char) =>
    char.charCodeAt(0),
  );
  const url = URL.createObjectURL(
    new Blob([data], { type: "application/octet-stream" }),
  );
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = value.name;
  anchor.click();
  URL.revokeObjectURL(url);
}
</script>
<template>
  <section class="space-y-4">
    <UiAlert v-if="error" tone="danger"
      >{{ error
      }}<UiButton variant="ghost" :action="load">{{
        $t("email.retry")
      }}</UiButton></UiAlert
    ><UiAlert v-if="success" tone="success">{{ success }}</UiAlert>
    <p v-if="loading && !query.trim()" role="status" class="text-sm text-muted">
      {{ $t("email.loading") }}
    </p>
    <div class="space-y-6">
      <UiCard
        :title="$t('email.messages')"
        class="h-full"
        :aria-busy="mailLoading"
      >
        <div class="mb-6">
          <UiInput
            v-model="query"
            :label="$t('email.search')"
            :help="$t('email.searchHelp')"
            type="search"
            maxlength="512"
          />
        </div>
        <UiEmptyState
          v-if="!mailLoading && !messages.length && !nextBefore"
          :title="
            query.trim() ? $t('email.noSearchResults') : $t('email.noMessages')
          "
          :description="
            query.trim()
              ? $t('email.noSearchResultsHelp')
              : $t('email.noMessagesHelp')
          "
        />
        <p
          v-if="mailLoading && query.trim()"
          role="status"
          class="mb-4 text-sm text-muted"
        >
          {{ $t("email.searching") }}
        </p>
        <p v-if="searchLimited" role="status" class="mb-4 text-sm text-muted">
          {{ $t("email.searchLimited") }}
        </p>
        <div
          ref="messageList"
          role="region"
          :aria-label="$t('email.messageList')"
          tabindex="0"
          class="max-h-96 space-y-2 overflow-y-auto overscroll-contain"
          @scroll.passive="moreMessages"
        >
          <div
            v-for="message in messages"
            :key="message.id"
            class="flex min-w-0 items-center gap-2"
          >
            <UiButton
              variant="ghost"
              align="start"
              class="min-w-0 flex-1"
              @click="read(message)"
              ><span class="min-w-0 text-left"
                ><span class="block truncate font-medium">{{
                  message.message.subject
                }}</span
                ><span class="block truncate text-xs text-muted"
                  >{{ message.message.sender }} ·
                  {{ message.message.date }}</span
                ><UiBadge v-if="message.label" tone="neutral">{{
                  message.label
                }}</UiBadge></span
              ></UiButton
            >
            <UiIconButton
              :icon="ShareIcon"
              :label="
                $t('email.viewThread', { subject: message.message.subject })
              "
              variant="ghost"
              @click="threadMessage = message.id"
            />
          </div>
        </div>
      </UiCard>
      <UiCard
        :title="$t('email.drafts')"
        :description="$t('email.approvalHelp')"
        class="h-full"
      >
        <template #actions>
          <UiButton
            v-if="can(account, 'use')"
            variant="secondary"
            @click="edit()"
            >{{ $t("email.compose") }}</UiButton
          >
        </template>
        <UiEmptyState
          v-if="!drafts.length"
          :title="$t('email.noDrafts')"
          :description="$t('email.approvalHelp')"
        />
        <div
          ref="draftList"
          role="region"
          :aria-label="$t('email.draftList')"
          tabindex="0"
          class="max-h-96 overflow-y-auto overscroll-contain"
          @scroll.passive="moreDrafts"
        >
          <article
            v-for="item in drafts"
            :key="item.id"
            class="border-b border-border py-4 last:border-0"
          >
            <div class="flex flex-wrap items-center gap-2">
              <h3 class="font-medium">{{ item.content.subject }}</h3>
              <UiBadge
                :tone="
                  item.status === 'sent'
                    ? 'success'
                    : item.status === 'uncertain' || item.status === 'sending'
                      ? 'warning'
                      : 'neutral'
                "
                >{{ $t(`email.status.${item.status}`) }}</UiBadge
              >
            </div>
            <p class="mt-1 break-words text-xs text-muted">
              {{ item.content.to.join(", ") }}
            </p>
            <pre
              class="mt-3 max-h-64 overflow-auto whitespace-pre-wrap break-words font-sans text-sm"
              >{{ item.content.body }}</pre>
            <p
              v-if="item.content.attachments.length"
              class="mt-2 text-xs text-muted"
            >
              {{ item.content.attachments.map((file) => file.name).join(", ") }}
            </p>
            <UiAlert
              v-if="['uncertain', 'sending'].includes(item.status)"
              tone="warning"
              class="mt-3"
              >{{ $t("email.uncertain") }}
              <code class="break-all">{{ item.delivery_id }}</code></UiAlert
            >
            <div class="mt-3 flex flex-wrap gap-2">
              <UiButton
                v-if="
                  can(account, 'use') &&
                  ['draft', 'approved'].includes(item.status)
                "
                variant="secondary"
                @click="edit(item)"
                >{{ $t("email.editDraft") }}</UiButton
              ><UiButton
                v-if="can(account, 'approve') && item.status === 'draft'"
                variant="secondary"
                :loading="busy"
                @click="action('approve', item)"
                >{{ $t("email.approve") }}</UiButton
              ><UiButton
                v-if="can(account, 'send') && item.status === 'approved'"
                :loading="busy"
                @click="action('send', item)"
                >{{ $t("email.send") }}</UiButton
              >
            </div>
          </article>
        </div>
      </UiCard>
    </div>
    <UiDialog
      v-model:open="reader"
      @close="selected = undefined"
      :title="selected?.message.subject ?? ''"
      :description="selected?.message.sender"
      size="wide"
      :close-label="$t('actions.close')"
    >
      <template v-if="selected">
        <p class="text-xs text-muted">{{ selected.message.date }}</p>
        <p
          v-if="selected.message.thread_reference || selected.message.reference"
          class="mt-2 text-xs text-muted"
        >
          {{ $t("email.reference") }}
          <code class="break-all">{{
            selected.message.thread_reference || selected.message.reference
          }}</code>
        </p>
        <pre
          class="mt-3 max-h-96 overflow-auto whitespace-pre-wrap break-words font-sans text-sm"
          >{{ selected.message.body }}</pre>
        <div class="mt-3 flex flex-wrap gap-2">
          <UiButton
            v-for="attachment in selected.message.attachments"
            :key="attachment.name"
            variant="secondary"
            @click="download(attachment)"
            >{{ attachment.name }}</UiButton
          >
        </div>
        <div v-if="can(account, 'use')" class="mt-4 flex flex-wrap gap-2">
          <UiButton variant="secondary" @click="edit(undefined, selected)">{{
            $t("email.reply")
          }}</UiButton>
          <UiButton :loading="busy" @click="action('generate', selected)">{{
            $t("email.generate")
          }}</UiButton>
        </div>
      </template>
    </UiDialog>
    <LazyEmailThreadGraph
      v-if="threadMessage"
      :account="account.id"
      :message-id="threadMessage"
      @close="threadMessage = ''"
      @read="read"
    />
    <EmailComposer
      v-model:open="composer"
      :account="account"
      :draft="draft"
      :reply="reply"
      @saved="load()"
    />
  </section>
</template>
