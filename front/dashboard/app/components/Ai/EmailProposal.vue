<script setup lang="ts">
import { PaperAirplaneIcon, EnvelopeIcon } from "@heroicons/vue/24/outline";
import type { EmailProposal } from "~/utils/pengolo-activity";
import {
  parseAccounts,
  can,
  object,
  type EmailAccount,
  type EmailRight,
} from "~/utils/email";
const props = defineProps<{ proposal: EmailProposal }>();
const api = useEmailApi();
const statusApi = useEmailApi();
const { t } = useI18n();
const accountSession = useAccount();
const content = ref({
  ...props.proposal,
  to: [...props.proposal.to],
  cc: [...props.proposal.cc],
  bcc: [...props.proposal.bcc],
});
const trackOpens = ref(false);
const recipients = ref(props.proposal.to.join(", ")),
  cc = ref(props.proposal.cc.join(", ")),
  bcc = ref(props.proposal.bcc.join(", "));
const accounts = ref<EmailAccount[]>([]),
  selected = ref(""),
  choosing = ref(false),
  busy = ref(false),
  sent = ref(false),
  blocked = ref(true),
  checkingDelivery = ref(false),
  error = ref("");
let epoch = 0;
let statusVersion = 0;
let active = true;
function clear() {
  epoch++;
  statusVersion++;
  api.cancel();
  statusApi.cancel();
  accounts.value = [];
  selected.value = "";
  choosing.value = false;
  busy.value = false;
  error.value = "";
  sent.value = false;
  blocked.value = true;
  checkingDelivery.value = false;
}
async function deliveryStatus() {
  if (!active || !accountSession.session.value) return;
  const own = epoch,
    version = ++statusVersion;
  statusApi.cancel();
  checkingDelivery.value = true;
  try {
    const result = object(
      await statusApi.operation({
        operation: "proposal_status",
        draft: props.proposal.id,
      }),
    );
    if (own !== epoch || version !== statusVersion) return;
    sent.value = result.status === "sent";
    blocked.value = !["new", "draft", "approved", "sent"].includes(
      String(result.status),
    );
    error.value = ["sending", "uncertain"].includes(String(result.status))
      ? t("email.chat.uncertain")
      : blocked.value
        ? t("email.chat.statusUnavailable")
        : "";
  } catch {
    if (own === epoch && version === statusVersion) {
      blocked.value = true;
      error.value = t("email.chat.statusUnavailable");
    }
  } finally {
    if (own === epoch && version === statusVersion)
      checkingDelivery.value = false;
  }
}
const locked = computed(
  () => busy.value || sent.value || blocked.value || checkingDelivery.value,
);
watch(
  () => [
    accountSession.session.value?.subject,
    accountSession.session.value?.tenant,
  ],
  clear,
);
onMounted(deliveryStatus);
onDeactivated(() => {
  active = false;
  clear();
});
onActivated(() => {
  if (!active) {
    active = true;
    void deliveryStatus();
  }
});
onBeforeUnmount(clear);
const split = (value: string) =>
  value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
const valid = computed(() => {
  const to = split(recipients.value),
    copies = [...split(cc.value), ...split(bcc.value)];
  return (
    to.length > 0 &&
    to.length <= 20 &&
    split(cc.value).length <= 20 &&
    split(bcc.value).length <= 20 &&
    [...to, ...copies].every(
      (address) =>
        address.includes("@") &&
        address.length <= 320 &&
        !/[\x00-\x20\x7f]/.test(address),
    ) &&
    content.value.subject.trim().length > 0 &&
    content.value.subject.length <= 998 &&
    content.value.body.trim().length > 0 &&
    content.value.body.length <= 16384
  );
});
async function deliver() {
  if (locked.value || !valid.value) return;
  statusVersion++;
  statusApi.cancel();
  const own = epoch;
  busy.value = true;
  error.value = "";
  try {
    const required: EmailRight[] = ["use", "read", "approve", "send"];
    const available = parseAccounts(
      await api.operation({ operation: "accounts" }),
    ).filter(
      (account) =>
        account.config.enabled &&
        required.every((right) => can(account, right)),
    );
    if (own !== epoch) return;
    accounts.value = available;
    if (!available.length) {
      error.value = t("email.chat.noSender");
      return;
    }
    if (available.length === 1) selected.value = available[0]?.id ?? "";
    if (!available.some((account) => account.id === selected.value)) {
      choosing.value = true;
      return;
    }
    const account = selected.value;
    let draft: Record<string, unknown> | undefined;
    try {
      draft = object(
        await api.operation({
          operation: "draft",
          account,
          draft: props.proposal.id,
        }),
      );
    } catch {
      if (own !== epoch) return;
    }
    if (draft?.status === "sent") {
      sent.value = true;
      choosing.value = false;
      return;
    }
    if (draft && ["sending", "uncertain"].includes(String(draft.status)))
      throw new Error("UNCERTAIN");
    const payload = {
      track_opens: trackOpens.value,
      to: split(recipients.value),
      cc: split(cc.value),
      bcc: split(bcc.value),
      subject: content.value.subject,
      body: content.value.body,
      reply_to_message_id: "",
      references: "",
      attachments: [],
    };
    const saved = object(
      await api.operation({
        operation: "save_draft",
        account,
        draft: props.proposal.id,
        ...(draft ? { revision: draft.revision } : {}),
        content: payload,
      }),
    );
    if (own !== epoch) return;
    await api.operation({
      operation: "approve",
      account,
      draft: props.proposal.id,
      revision: saved.revision,
    });
    if (own !== epoch) return;
    await api.operation({
      operation: "send",
      account,
      draft: props.proposal.id,
      revision: saved.revision,
    });
    if (own !== epoch) return;
    sent.value = true;
    choosing.value = false;
  } catch (failure) {
    if (own === epoch) {
      blocked.value = true;
      error.value = t(
        failure instanceof Error && failure.message === "UNCERTAIN"
          ? "email.chat.uncertain"
          : "email.chat.failed",
      );
    }
  } finally {
    if (own === epoch) busy.value = false;
  }
}
</script>
<template>
  <UiCard
    :title="content.subject"
    :description="$t('email.chat.help')"
    class="my-4"
  >
    <UiAlert v-if="error" tone="danger"
      >{{ error }}
      <NuxtLink
        v-if="!accounts.length && !blocked && !checkingDelivery"
        to="/settings/user#email"
        class="underline"
        >{{ $t("email.connect") }}</NuxtLink
      ></UiAlert
    >
    <UiAlert v-if="sent" tone="success">{{ $t("email.chat.sent") }}</UiAlert>
    <p v-if="checkingDelivery" role="status" class="text-sm text-muted">
      {{ $t("email.chat.checkingStatus") }}
    </p>
    <UiButton
      v-if="blocked && error"
      variant="ghost"
      :loading="checkingDelivery"
      :action="deliveryStatus"
      >{{ $t("email.chat.checkStatus") }}</UiButton
    >
    <div class="mt-3 grid gap-3 sm:grid-cols-2">
      <div class="sm:col-span-2">
        <UiInput
          v-model="recipients"
          :label="$t('email.recipients')"
          :disabled="locked"
        />
      </div>
      <UiInput v-model="cc" :label="$t('email.cc')" :disabled="locked" />
      <UiInput v-model="bcc" :label="$t('email.bcc')" :disabled="locked" />
      <div class="sm:col-span-2">
        <UiInput
          v-model="content.subject"
          :label="$t('email.subject')"
          :disabled="locked"
        />
      </div>
      <div class="sm:col-span-2">
        <UiTextarea
          v-model="content.body"
          :label="$t('email.body')"
          :disabled="locked"
          :rows="6"
        />
      </div>
      <div class="sm:col-span-2">
        <EmailTrackingOption v-model="trackOpens" :disabled="locked" />
      </div>
    </div>
    <UiButton
      class="mt-3"
      :loading="busy"
      :disabled="locked || !valid"
      @click="deliver"
      ><PaperAirplaneIcon aria-hidden="true" />{{
        $t(sent ? "email.status.sent" : "email.chat.send")
      }}</UiButton
    >
    <UiDialog
      v-model:open="choosing"
      :title="$t('email.chat.chooseSender')"
      :dismissible="!busy"
    >
      <UiSelect
        v-model="selected"
        :label="$t('email.chat.sender')"
        :options="
          accounts.map((account) => ({
            value: account.id,
            label: account.config.address,
          }))
        "
        :disabled="busy"
      />
      <template #footer
        ><UiButton :disabled="!selected" :loading="busy" @click="deliver"
          ><EnvelopeIcon aria-hidden="true" />{{
            $t("email.chat.send")
          }}</UiButton
        ></template
      >
    </UiDialog>
  </UiCard>
</template>
