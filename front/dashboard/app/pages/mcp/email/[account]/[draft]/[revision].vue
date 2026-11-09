<script setup lang="ts">
import {
  can,
  parseAccounts,
  parseDrafts,
  type EmailAccount,
  type EmailDraft,
} from "~/utils/email";

const route = useRoute();
const identity = useAccount();
const api = useEmailApi();
const { t } = useI18n();
const account = shallowRef<EmailAccount>();
const draft = shallowRef<EmailDraft>();
const reviewed = ref(false),
  busy = ref(false),
  loading = ref(false),
  error = ref("");
const sent = computed(() => draft.value?.status === "sent");
const ready = computed(
  () =>
    !!account.value &&
    !!draft.value &&
    ["draft", "approved"].includes(draft.value.status),
);
let epoch = 0,
  active = true;

function clear() {
  epoch++;
  api.cancel();
  account.value = undefined;
  draft.value = undefined;
  reviewed.value = false;
}

/** A link identifies a revision; it never grants approval or selects a later edit. */
async function load() {
  clear();
  if (!active) return;
  const own = epoch;
  loading.value = true;
  error.value = "";
  try {
    const { account: accountId, draft: draftId, revision } = route.params;
    if (
      typeof accountId !== "string" ||
      !/^[a-f0-9]{32}$/.test(accountId) ||
      typeof draftId !== "string" ||
      !/^[a-f0-9]{32}$/.test(draftId) ||
      typeof revision !== "string" ||
      !/^[1-9][0-9]*$/.test(revision) ||
      !Number.isSafeInteger(Number(revision))
    )
      throw new Error("Invalid review link");
    const [accounts, value] = await Promise.all([
      api.operation({ operation: "accounts" }),
      api.operation({ operation: "draft", account: accountId, draft: draftId }),
    ]);
    if (own !== epoch || !active) return;
    const sender = parseAccounts(accounts).find(
      (item) => item.id === accountId,
    );
    const content = parseDrafts([value])[0];
    if (
      !sender ||
      !can(sender, "read") ||
      !can(sender, "approve") ||
      !can(sender, "send") ||
      !content ||
      content.id !== draftId ||
      content.revision !== Number(revision)
    )
      throw new Error("Review unavailable");
    account.value = sender;
    draft.value = content;
    if (!["draft", "approved", "sent"].includes(content.status))
      error.value = t("email.uncertain");
  } catch {
    if (own === epoch && active) error.value = t("mcp.emailReview.unavailable");
  } finally {
    if (own === epoch) loading.value = false;
  }
}

/** Approval and delivery are primary-session actions bound to the displayed revision. */
async function deliver() {
  if (
    busy.value ||
    !ready.value ||
    !reviewed.value ||
    !draft.value ||
    !account.value
  )
    return;
  const own = epoch;
  const request = {
    account: account.value.id,
    draft: draft.value.id,
    revision: draft.value.revision,
  };
  busy.value = true;
  error.value = "";
  try {
    await api.operation({ operation: "approve", ...request });
    if (own !== epoch || !active) return;
    await api.operation({ operation: "send", ...request });
    if (own === epoch && active && draft.value)
      draft.value = { ...draft.value, status: "sent" };
  } catch {
    if (own === epoch && active) {
      reviewed.value = false;
      await load();
      error.value ||= t("email.sendError");
    }
  } finally {
    busy.value = false;
  }
}

watch(
  () => route.fullPath,
  () => void load(),
);
watch(
  () => [identity.session.value?.subject, identity.session.value?.tenant],
  () => void load(),
);
onMounted(load);
onBeforeUnmount(clear);
onDeactivated(() => {
  active = false;
  clear();
});
onActivated(() => {
  if (!active) {
    active = true;
    void load();
  }
});
</script>

<template>
  <main class="mx-auto w-full max-w-3xl p-6">
    <UiCard class="space-y-5">
      <h1 class="text-xl font-semibold">{{ $t("mcp.emailReview.title") }}</h1>
      <p v-if="loading" role="status" class="text-sm text-muted">
        {{ $t("email.loading") }}
      </p>
      <UiAlert v-if="error" tone="danger">{{ error }}</UiAlert>
      <template v-if="draft && account">
        <h2 class="text-lg font-semibold">{{ draft.content.subject }}</h2>
        <dl class="space-y-2 break-words text-sm">
          <div>
            <dt class="font-medium">{{ $t("email.chat.sender") }}</dt>
            <dd>{{ account.config.address }}</dd>
          </div>
          <div>
            <dt class="font-medium">{{ $t("email.recipients") }}</dt>
            <dd>{{ draft.content.to.join(", ") }}</dd>
          </div>
          <div v-if="draft.content.cc.length">
            <dt class="font-medium">{{ $t("email.cc") }}</dt>
            <dd>{{ draft.content.cc.join(", ") }}</dd>
          </div>
          <div v-if="draft.content.bcc?.length">
            <dt class="font-medium">{{ $t("email.bcc") }}</dt>
            <dd>{{ draft.content.bcc.join(", ") }}</dd>
          </div>
        </dl>
        <p class="whitespace-pre-wrap break-words text-sm leading-6">
          {{ draft.content.body }}
        </p>
        <ul v-if="draft.content.attachments.length" class="space-y-2 text-sm">
          <li
            v-for="(file, index) in draft.content.attachments"
            :key="index"
            class="rounded-control border border-border px-3 py-2"
          >
            {{ file.name }}
          </li>
        </ul>
        <p v-if="sent" role="status" class="text-sm text-success">
          {{ $t("email.chat.sent") }}
        </p>
        <template v-else-if="ready">
          <UiCheckbox
            v-model="reviewed"
            :disabled="busy"
            :label="$t('mcp.emailReview.reviewed')"
          />
          <UiButton :disabled="!reviewed" :loading="busy" @click="deliver">{{
            $t("mcp.emailReview.send")
          }}</UiButton>
        </template>
      </template>
    </UiCard>
  </main>
</template>
