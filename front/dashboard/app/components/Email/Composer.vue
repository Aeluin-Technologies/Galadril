<script setup lang="ts">
import type {
  EmailAccount,
  EmailDraft,
  EmailDraftContent,
  EmailMessage,
} from "~/utils/email";
const props = defineProps<{
  account: EmailAccount;
  draft?: EmailDraft;
  reply?: EmailMessage;
}>();
const open = defineModel<boolean>("open", { default: false });
const emit = defineEmits<{ saved: [] }>();
const api = useEmailApi();
const { t } = useI18n();
const loading = ref(false),
  error = ref(""),
  recipients = ref(""),
  cc = ref(""),
  bcc = ref("");
const empty = (): EmailDraftContent => ({
  track_opens: false,
  to: [],
  cc: [],
  bcc: [],
  subject: "",
  body: "",
  reply_to_message_id: "",
  references: "",
  attachments: [],
});
const content = ref<EmailDraftContent>(empty());
const readingAttachments = ref(false);
watch(open, (value) => {
  if (!value) {
    content.value = empty();
    error.value = "";
    return;
  }
  content.value = props.draft
    ? {
        ...props.draft.content,
        to: [...props.draft.content.to],
        cc: [...props.draft.content.cc],
        attachments: props.draft.content.attachments.map((file) => ({
          ...file,
        })),
      }
    : empty();
  if (props.reply && !props.draft) {
    const message = props.reply.message;
    content.value.to = [message.sender];
    content.value.subject = message.subject.startsWith("Re:")
      ? message.subject
      : `Re: ${message.subject}`;
    content.value.reply_to_message_id = message.message_id;
    content.value.reference = message.reference;
    content.value.references = message.references;
  }
  recipients.value = content.value.to.join(", ");
  cc.value = content.value.cc.join(", ");
  bcc.value = (content.value.bcc ?? []).join(", ");
});
async function save() {
  if (loading.value || readingAttachments.value) return;
  loading.value = true;
  error.value = "";
  try {
    content.value.to = recipients.value
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
    content.value.cc = cc.value
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
    content.value.bcc = bcc.value
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
    await api.operation({
      operation: "save_draft",
      account: props.account.id,
      content: content.value,
      ...(props.draft
        ? { draft: props.draft.id, revision: props.draft.revision }
        : {}),
    });
    open.value = false;
    emit("saved");
  } catch {
    error.value = t("email.saveError");
  } finally {
    loading.value = false;
  }
}
onDeactivated(() => {
  open.value = false;
  content.value = empty();
});
</script>
<template>
  <UiDialog
    v-model:open="open"
    :title="$t('email.compose')"
    :description="$t('email.approvalHelp')"
    :dismissible="!loading"
    :close-label="$t('actions.close')"
  >
    <form id="email-draft" class="space-y-4" @submit.prevent="save">
      <UiAlert v-if="error" tone="danger">{{ error }}</UiAlert>
      <UiInput
        v-model="recipients"
        :label="$t('email.recipients')"
        required
        autocomplete="off"
      />
      <UiInput v-model="cc" :label="$t('email.cc')" autocomplete="off" />
      <UiInput v-model="bcc" :label="$t('email.bcc')" autocomplete="off" />
      <UiInput
        v-model="content.subject"
        :label="$t('email.subject')"
        required
      />
      <UiTextarea
        v-model="content.body"
        :label="$t('email.body')"
        :rows="12"
        required
      />
      <EmailTrackingOption v-model="content.track_opens" :disabled="loading" />
      <EmailAttachments
        v-model="content.attachments"
        v-model:reading="readingAttachments"
        :disabled="loading"
      />
    </form>
    <template #footer
      ><UiButton variant="secondary" @click="open = false">{{
        $t("actions.cancel")
      }}</UiButton
      ><UiButton
        type="submit"
        form="email-draft"
        :loading="loading"
        :disabled="readingAttachments"
        :pending-label="$t('email.saving')"
        >{{ $t("email.saveDraft") }}</UiButton
      ></template
    >
  </UiDialog>
</template>
