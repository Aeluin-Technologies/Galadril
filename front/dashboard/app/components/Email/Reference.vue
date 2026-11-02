<script setup lang="ts">
import {
  can,
  parseAccounts,
  parseMessagePage,
  parseMessages,
} from "~/utils/email";
import type { EmailMessage } from "~/utils/email";
const props = defineProps<{ reference: string }>();
const emit = defineEmits<{ close: [] }>();
const api = useEmailApi();
const identity = useAccount();
const open = ref(true),
  loading = ref(false),
  failed = ref(false);
const message = shallowRef<EmailMessage>();
const accountId = ref("");
let epoch = 0;
function clear() {
  epoch++;
  api.cancel();
  message.value = undefined;
  accountId.value = "";
}
async function load() {
  clear();
  const own = epoch;
  loading.value = true;
  failed.value = false;
  try {
    if (!/^GDR-[A-F0-9]{32}$/.test(props.reference))
      throw new Error("EMAIL_REFERENCE_INVALID");
    const accounts = parseAccounts(
      await api.operation({ operation: "accounts" }),
    );
    for (const account of accounts
      .filter((value) => can(value, "read"))
      .slice(0, 16)) {
      let before = 0;
      for (let page = 0; page < 8; page++) {
        const result = parseMessagePage(
          await api.operation({
            operation: "messages",
            account: account.id,
            query: props.reference,
            before,
          }),
        );
        if (own !== epoch) return;
        const match = result.items.find(
          (value) => value.message.reference === props.reference,
        );
        if (match) {
          const content = await api.operation({
            operation: "message",
            account: account.id,
            message: match.id,
          });
          const read = parseMessages([{ ...match, message: content }])[0];
          if (own !== epoch) return;
          if (!read) throw new Error("EMAIL_REFERENCE_UNAVAILABLE");
          message.value = read;
          accountId.value = account.id;
          return;
        }
        if (!result.next_before || result.next_before === before) break;
        before = result.next_before;
      }
    }
    throw new Error("EMAIL_REFERENCE_UNAVAILABLE");
  } catch {
    if (own === epoch) failed.value = true;
  } finally {
    if (own === epoch) loading.value = false;
  }
}
watch(
  () => props.reference,
  () => void load(),
  { immediate: true },
);
watch(
  () => [identity.session.value?.tenant, identity.session.value?.subject],
  () => {
    clear();
    emit("close");
  },
);
onBeforeUnmount(clear);
onDeactivated(clear);
</script>
<template>
  <UiDialog
    v-model:open="open"
    size="wide"
    :title="message?.message.subject ?? $t('email.referenceTitle')"
    :description="message?.message.sender ?? reference"
    @close="emit('close')"
  >
    <p v-if="loading" role="status">{{ $t("email.referenceLoading") }}</p>
    <div v-if="failed" role="alert" class="text-sm text-danger">
      {{ $t("email.referenceUnavailable") }}
      <UiButton variant="ghost" @click="load">{{
        $t("pipeline.library.retry")
      }}</UiButton>
    </div>
    <template v-if="message">
      <p class="mb-3 text-xs text-muted">{{ message.message.date }}</p>
      <pre
        class="max-h-96 overflow-auto whitespace-pre-wrap break-words font-sans text-sm"
        >{{ message.message.body }}</pre>
      <LazyEmailConversation :account="accountId" :message-id="message.id" />
    </template>
  </UiDialog>
</template>
