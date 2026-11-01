<script setup lang="ts">
import { ArrowPathIcon, PlusIcon } from "@heroicons/vue/24/outline";
import type { EmailAccount, EmailAccountAction } from "~/utils/email";
import { parseAccounts, can, rows, text, object } from "~/utils/email";
defineProps<{ embedded?: boolean }>();
const accountSession = useAccount();
const api = useEmailApi();
const { t } = useI18n();
const route = useRoute();
const router = useRouter();
const accounts = ref<EmailAccount[]>([]),
  selectedId = ref(""),
  providers = ref<string[]>([]);
const selected = computed(() =>
  accounts.value.find((account) => account.id === selectedId.value),
);
const view = computed(() => {
  const requested = route.query.view;
  if (requested === "messages" && selected.value && can(selected.value, "read"))
    return "messages";
  if (requested === "replies" && selected.value && can(selected.value, "use"))
    return "replies";
  if (
    requested !== "connection" &&
    selected.value &&
    can(selected.value, "read")
  )
    return "messages";
  return "connection";
});
const loading = ref(true),
  busy = ref(false),
  error = ref(""),
  success = ref("");
const form = ref(false),
  editing = ref<EmailAccount>(),
  access = ref(false),
  deleting = ref(false);
const editMode = ref<"identity" | "servers" | "collection">("identity");
const oauthAddress = ref("");
const secretOpen = ref(false),
  revealed = ref<{
    username: string;
  }>();
let timer: ReturnType<typeof setTimeout> | undefined;
let epoch = 0;
let mounted = false;
function clearSecret() {
  revealed.value = undefined;
  secretOpen.value = false;
  if (timer) clearTimeout(timer);
  timer = undefined;
}
watch(
  () => route.query.account,
  (id) => {
    selectedId.value = typeof id === "string" ? id : "";
    clearSecret();
    editing.value = undefined;
    form.value = false;
    access.value = false;
    deleting.value = false;
    error.value = "";
    success.value = "";
  },
  { immediate: true },
);
watch(secretOpen, (value) => {
  if (!value) clearSecret();
});
async function load() {
  const own = ++epoch;
  loading.value = true;
  error.value = "";
  try {
    const [list, available] = await Promise.all([
      api.operation({ operation: "accounts" }),
      api.operation({ operation: "providers" }),
    ]);
    if (own !== epoch) return;
    accounts.value = parseAccounts(list);
    providers.value = rows(available).map(text);
    if (!accounts.value.some((account) => account.id === selectedId.value))
      selectedId.value = "";
  } catch {
    if (own === epoch) error.value = t("email.loadError");
  } finally {
    if (own === epoch) loading.value = false;
  }
}
async function initialize() {
  const code = route.query.code,
    state = route.query.state;
  const rejected = route.query.error;
  if (typeof code === "string" && typeof state === "string") {
    await router.replace({ query: {} });
    try {
      await api.operation({ operation: "oauth_finish", code, state });
      success.value = t("email.connected");
    } catch {
      error.value = t("email.consentError");
    }
  } else if (rejected) {
    await router.replace({ query: {} });
    error.value = t("email.consentError");
  }
  await load();
  mounted = true;
}
onMounted(initialize);
onActivated(() => {
  if (mounted) load();
});
function clearWorkspace() {
  epoch++;
  api.cancel();
  clearSecret();
  accounts.value = [];
  selectedId.value = "";
  editing.value = undefined;
  form.value = false;
  access.value = false;
  deleting.value = false;
  error.value = "";
  success.value = "";
}
onDeactivated(clearWorkspace);
watch(
  () =>
    [
      accountSession.session.value?.subject,
      accountSession.session.value?.tenant,
    ].join(":"),
  (value, previous) => {
    if (previous !== ":" && value !== previous) {
      clearWorkspace();
      if (value !== ":") load();
    }
  },
);
onBeforeUnmount(clearSecret);
function connect() {
  editing.value = undefined;
  form.value = true;
}
function edit(mode: "identity" | "servers" | "collection" = "identity") {
  editMode.value = mode;
  editing.value = selected.value;
  form.value = true;
}
function accountAction(action: EmailAccountAction | "sync") {
  if (action === "edit") edit();
  else if (action === "servers" || action === "collection") edit(action);
  else if (action === "access") access.value = true;
  else if (action === "delete") deleting.value = true;
  else void perform(action);
}
async function perform(operation: string) {
  const account = selected.value;
  if (!account || busy.value) return;
  busy.value = true;
  error.value = "";
  success.value = "";
  const own = epoch;
  try {
    const result = await api.operation({
      operation,
      account: account.id,
      ...(operation === "delete" ? { revision: account.revision } : {}),
    });
    if (own !== epoch || selectedId.value !== account.id) return;
    if (operation === "reveal") {
      const value = object(result);
      revealed.value = {
        username: text(value.username),
      };
      secretOpen.value = true;
      timer = setTimeout(clearSecret, 60000);
    } else {
      deleting.value = false;
      success.value = t("email.actionComplete");
      await load();
    }
  } catch (failure) {
    if (own === epoch && selectedId.value === account.id) {
      if (operation === "test") {
        await load();
        if (epoch !== own + 1 || selectedId.value !== account.id) return;
      }
      error.value = t(
        failure instanceof Error && failure.message === "AUTHENTICATION_FAILED"
          ? "email.authenticationFailed"
          : "email.actionError",
      );
    }
  } finally {
    busy.value = false;
  }
}
async function consent(provider: string) {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    const response = object(
      await api.operation({
        operation: "oauth_start",
        provider,
        address: oauthAddress.value,
      }),
    );
    const url = new URL(text(response.url));
    if (
      url.protocol !== "https:" ||
      !["accounts.google.com", "login.microsoftonline.com"].includes(
        url.hostname,
      )
    )
      throw new Error("provider");
    await navigateTo(url.href, { external: true });
  } catch {
    error.value = t("email.consentError");
  } finally {
    busy.value = false;
  }
}
</script>
<template>
  <div
    :class="
      embedded ? 'space-y-6' : 'mx-auto max-w-7xl space-y-6 px-4 py-8 sm:px-6'
    "
  >
    <UiPageHeader
      :title="$t('email.title')"
      :description="$t('email.description')"
      ><template v-if="!selected" #actions
        ><UiButton
          variant="secondary"
          :action="load"
          :pending-label="$t('email.loading')"
          ><ArrowPathIcon class="h-4 w-4" />{{ $t("email.refresh") }}</UiButton
        ><UiButton @click="connect"
          ><PlusIcon class="h-4 w-4" />{{ $t("email.connect") }}</UiButton
        ></template
      ></UiPageHeader
    >
    <NuxtLink
      to="/ontology?package=utils.mail"
      class="inline-flex rounded-control text-sm text-primary-foreground underline underline-offset-4 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2"
      >{{ $t("ontologyManager.emailDataModel") }}</NuxtLink
    >
    <UiAlert
      v-if="error"
      tone="danger"
      class="flex flex-wrap items-center justify-between gap-3"
      >{{ error
      }}<UiButton variant="ghost" :action="load">{{
        $t("email.retry")
      }}</UiButton></UiAlert
    ><UiAlert v-if="success" tone="success">{{ success }}</UiAlert>
    <UiCard
      v-if="providers.length"
      :title="$t('email.providerConnect')"
      :description="$t('email.consentHelp')"
      ><div class="flex flex-wrap items-end gap-3">
        <UiInput
          v-model="oauthAddress"
          type="email"
          :label="$t('email.address')"
          autocomplete="email"
        /><UiButton
          v-for="provider in providers"
          :key="provider"
          :loading="busy"
          :disabled="!oauthAddress"
          @click="consent(provider)"
          >{{
            provider === "google" ? $t("email.google") : $t("email.microsoft")
          }}</UiButton
        >
      </div></UiCard
    >
    <p v-if="loading" role="status" class="text-sm text-muted">
      {{ $t("email.loading") }}
    </p>
    <UiEmptyState
      v-if="!loading && !accounts.length"
      :title="$t('email.noAccounts')"
      :description="$t('email.noAccountsHelp')"
      ><template #action
        ><UiButton @click="connect">{{
          $t("email.connect")
        }}</UiButton></template
      ></UiEmptyState
    >
    <EmailAccountList
      v-if="accounts.length && !selected"
      :accounts="accounts"
    />
    <div v-if="selected" :key="selected.id" class="min-w-0 space-y-6">
      <EmailAccountHeader
        :account="selected"
        :view="view"
        :busy="busy"
        @action="accountAction"
      />
      <EmailAccountSettings
        v-if="view === 'connection'"
        :account="selected"
        :busy="busy"
        @action="accountAction"
      />
      <EmailMailbox
        v-else-if="view === 'messages' && can(selected, 'read')"
        :account="selected"
      />
      <EmailProfile
        v-else-if="view === 'replies' && can(selected, 'use')"
        :account="selected"
      />
      <EmailAccess
        v-if="can(selected, 'share')"
        v-model:open="access"
        :account="selected"
      />
    </div>
    <EmailConnectWizard
      v-if="!editing"
      v-model:open="form"
      :providers="providers"
      @saved="load"
      @oauth="
        (provider: string, address: string) => {
          oauthAddress = address;
          consent(provider);
        }
      "
    />
    <EmailConnectionForm
      v-else
      v-model:open="form"
      :account="editing"
      :mode="editMode"
      @saved="load"
    />
    <UiDialog
      v-model:open="deleting"
      :title="$t('email.delete')"
      :description="$t('email.deleteHelp')"
      :close-label="$t('actions.close')"
      ><template #footer
        ><UiButton variant="secondary" @click="deleting = false">{{
          $t("actions.cancel")
        }}</UiButton
        ><UiButton
          variant="danger"
          :loading="busy"
          @click="perform('delete')"
          >{{ $t("email.deleteConfirm") }}</UiButton
        ></template
      ></UiDialog
    >
    <UiDialog
      v-model:open="secretOpen"
      :title="$t('email.reveal')"
      :description="$t('email.revealHelp')"
      :close-label="$t('actions.close')"
      ><div v-if="revealed" class="space-y-4">
        <UiInput
          :model-value="revealed.username"
          :label="$t('email.username')"
          readonly
          autocomplete="off"
        /></div
    ></UiDialog>
  </div>
</template>
