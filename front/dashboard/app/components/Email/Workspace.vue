<script setup lang="ts">
import {
  EnvelopeIcon,
  ArrowPathIcon,
  PlusIcon,
} from "@heroicons/vue/24/outline";
import type { EmailAccount } from "~/utils/email";
import { parseAccounts, can, rows, text, object } from "~/utils/email";
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
const loading = ref(true),
  busy = ref(false),
  error = ref(""),
  success = ref("");
const form = ref(false),
  editing = ref<EmailAccount>(),
  access = ref(false),
  deleting = ref(false);
const oauthAddress = ref("");
const secretOpen = ref(false),
  revealed = ref<{
    username: string;
    password: string;
    refresh_token: string;
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
      selectedId.value = accounts.value[0]?.id ?? "";
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
function edit() {
  editing.value = selected.value;
  form.value = true;
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
    if (own !== epoch) return;
    if (operation === "reveal") {
      const value = object(result);
      revealed.value = {
        username: text(value.username),
        password: text(value.password),
        refresh_token:
          value.refresh_token === null ? "" : text(value.refresh_token),
      };
      secretOpen.value = true;
      timer = setTimeout(clearSecret, 60000);
    } else {
      deleting.value = false;
      success.value = t("email.actionComplete");
      await load();
    }
  } catch {
    if (own === epoch) error.value = t("email.actionError");
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
  <div class="mx-auto max-w-7xl space-y-6 px-4 py-8 sm:px-6">
    <UiPageHeader
      :title="$t('email.title')"
      :description="$t('email.description')"
      ><template #actions
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
    <div
      v-if="accounts.length"
      class="grid gap-6 lg:grid-cols-[16rem_minmax(0,1fr)]"
    >
      <aside class="space-y-2" :aria-label="$t('email.title')">
        <UiButton
          v-for="account in accounts"
          :key="account.id"
          :variant="selectedId === account.id ? 'secondary' : 'ghost'"
          align="start"
          class="w-full"
          @click="
            selectedId = account.id;
            clearSecret();
          "
          ><EnvelopeIcon class="h-4 w-4 shrink-0" /><span
            class="min-w-0 text-left"
            ><span class="block truncate font-medium">{{
              account.config.name
            }}</span
            ><span class="block truncate text-xs text-muted">{{
              account.config.address
            }}</span></span
          ></UiButton
        >
      </aside>
      <div v-if="selected" :key="selected.id" class="min-w-0 space-y-6">
        <UiCard
          :title="selected.config.name"
          :description="selected.config.address"
          ><div class="flex flex-wrap items-center gap-2">
            <UiBadge
              :tone="
                selected.sync_status === 'ok'
                  ? 'success'
                  : selected.sync_status === 'needs_attention'
                    ? 'warning'
                    : 'neutral'
              "
              >{{ $t(`email.sync.${selected.sync_status}`) }}</UiBadge
            ><UiBadge v-if="!selected.config.enabled" tone="warning">{{
              $t("email.disabled")
            }}</UiBadge>
          </div>
          <div class="mt-4 flex flex-wrap gap-2">
            <UiButton
              v-if="can(selected, 'use')"
              variant="secondary"
              :loading="busy"
              @click="perform('test')"
              >{{ $t("email.test") }}</UiButton
            ><UiButton
              v-if="can(selected, 'use')"
              variant="secondary"
              :loading="busy"
              @click="perform('sync')"
              >{{ $t("email.syncNow") }}</UiButton
            ><UiButton
              v-if="can(selected, 'edit')"
              variant="secondary"
              @click="edit"
              >{{ $t("email.editConnection") }}</UiButton
            ><UiButton
              v-if="can(selected, 'share')"
              variant="secondary"
              @click="access = true"
              >{{ $t("email.manageAccess") }}</UiButton
            ><UiButton
              v-if="can(selected, 'reveal')"
              variant="ghost"
              :loading="busy"
              @click="perform('reveal')"
              >{{ $t("email.reveal") }}</UiButton
            ><UiButton
              v-if="can(selected, 'delete')"
              variant="danger"
              @click="deleting = true"
              >{{ $t("email.delete") }}</UiButton
            >
          </div></UiCard
        >
        <EmailMailbox v-if="can(selected, 'read')" :account="selected" />
        <EmailProfile v-if="can(selected, 'use')" :account="selected" />
        <EmailAccess
          v-if="can(selected, 'share')"
          v-model:open="access"
          :account="selected"
        />
      </div>
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
        /><UiInput
          :model-value="revealed.password"
          :label="$t('email.password')"
          readonly
          autocomplete="off"
        /><UiInput
          v-if="revealed.refresh_token"
          :model-value="revealed.refresh_token"
          :label="$t('email.refreshToken')"
          readonly
          autocomplete="off"
        /></div
    ></UiDialog>
  </div>
</template>
