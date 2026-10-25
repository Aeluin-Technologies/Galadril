<script setup lang="ts">
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
} from "~/utils/email";
const props = defineProps<{ account: EmailAccount }>();
const api = useEmailApi();
const { t } = useI18n();
const messages = ref<EmailMessage[]>([]),
  drafts = ref<EmailDraft[]>([]),
  query = ref(""),
  selected = ref<EmailMessage>(),
  draft = ref<EmailDraft>(),
  reply = ref<EmailMessage>();
const nextBefore = ref(0);
const composer = ref(false),
  error = ref(""),
  success = ref(""),
  loading = ref(false),
  busy = ref(false);
let epoch = 0;
async function load(before = 0) {
  const own = ++epoch;
  loading.value = true;
  error.value = "";
  try {
    const [mail, outgoing] = await Promise.all([
      api.operation({
        operation: "messages",
        account: props.account.id,
        query: query.value,
        before,
      }),
      api.operation({ operation: "drafts", account: props.account.id }),
    ]);
    if (own !== epoch) return;
    const page = parseMessagePage(mail);
    messages.value = before ? [...messages.value, ...page.items] : page.items;
    nextBefore.value = page.next_before;
    drafts.value = parseDrafts(outgoing);
  } catch {
    if (own === epoch) error.value = t("email.loadError");
  } finally {
    if (own === epoch) loading.value = false;
  }
}
watch(
  () => props.account.id,
  () => {
    selected.value = undefined;
    messages.value = [];
    drafts.value = [];
    load();
  },
  { immediate: true },
);
onDeactivated(() => {
  epoch++;
  api.cancel();
  loading.value = false;
  selected.value = undefined;
  messages.value = [];
  drafts.value = [];
  composer.value = false;
});
onActivated(() => {
  if (!loading.value) load();
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
async function read(value: EmailMessage) {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    const message = await api.operation({
      operation: "message",
      account: props.account.id,
      message: value.id,
    });
    selected.value = parseMessages([{ ...value, message }])[0];
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
    <UiAlert v-if="error" tone="danger">{{ error }}</UiAlert
    ><UiAlert v-if="success" tone="success">{{ success }}</UiAlert>
    <div class="flex flex-wrap items-end gap-3">
      <form class="flex flex-1 items-end gap-2" @submit.prevent="load()">
        <UiInput
          v-model="query"
          :label="$t('email.search')"
          class="min-w-0 flex-1"
        /><UiButton type="submit" :loading="loading">{{
          $t("email.searchAction")
        }}</UiButton>
      </form>
      <UiButton
        v-if="can(account, 'use')"
        variant="secondary"
        @click="edit()"
        >{{ $t("email.compose") }}</UiButton
      >
    </div>
    <p v-if="loading" role="status" class="text-sm text-muted">
      {{ $t("email.loading") }}
    </p>
    <div class="grid gap-4 lg:grid-cols-2">
      <UiCard :title="$t('email.messages')">
        <UiEmptyState
          v-if="!loading && !messages.length"
          :title="$t('email.noMessages')"
          :description="$t('email.noMessagesHelp')"
        />
        <div class="divide-y divide-border">
          <UiButton
            v-for="message in messages"
            :key="message.id"
            variant="ghost"
            align="start"
            class="w-full"
            @click="read(message)"
            ><span class="min-w-0 text-left"
              ><span class="block truncate font-medium">{{
                message.message.subject
              }}</span
              ><span class="block truncate text-xs text-muted"
                >{{ message.message.sender }} · {{ message.message.date }}</span
              ><UiBadge v-if="message.label" tone="neutral">{{
                message.label
              }}</UiBadge></span
            ></UiButton
          >
        </div>
        <UiButton
          v-if="nextBefore > 0"
          variant="ghost"
          :action="() => load(nextBefore)"
          >{{ $t("email.loadMore") }}</UiButton
        >
      </UiCard>
      <UiCard
        v-if="selected"
        :title="selected.message.subject"
        :description="selected.message.sender"
      >
        <p class="text-xs text-muted">{{ selected.message.date }}</p>
        <p v-if="selected.message.reference" class="mt-2 text-xs text-muted">
          {{ $t("email.reference") }}
          <code class="break-all">{{ selected.message.reference }}</code>
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
          }}</UiButton
          ><UiButton :loading="busy" @click="action('generate', selected)">{{
            $t("email.generate")
          }}</UiButton>
        </div>
      </UiCard>
    </div>
    <UiCard :title="$t('email.drafts')" :description="$t('email.approvalHelp')">
      <UiEmptyState
        v-if="!drafts.length"
        :title="$t('email.noDrafts')"
        :description="$t('email.approvalHelp')"
      />
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
          >{{ $t("email.uncertain") }} {{ item.delivery_id }}</UiAlert
        >
        <div class="mt-3 flex flex-wrap gap-2">
          <UiButton
            v-if="
              can(account, 'use') && ['draft', 'approved'].includes(item.status)
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
    </UiCard>
    <EmailComposer
      v-model:open="composer"
      :account="account"
      :draft="draft"
      :reply="reply"
      @saved="load()"
    />
  </section>
</template>
