<script setup lang="ts">
import type { EmailAccount, EmailConfig, EmailEndpoint } from "~/utils/email";
import {
  connectionUpdate,
  validMailHost,
  parseSmtpPort,
  emailSmtpOptions,
} from "~/utils/email";

const props = withDefaults(
  defineProps<{
    account?: EmailAccount;
    mode?: "identity" | "servers" | "collection";
  }>(),
  { mode: "identity" },
);
const identity = computed(() => !props.account || props.mode === "identity");
const servers = computed(() => !props.account || props.mode === "servers");
const collection = computed(
  () =>
    (!props.account || props.mode === "collection") && !!config.value.imap_host,
);
const serversChanged = computed(
  () =>
    !!props.account &&
    (config.value.imap_host !== props.account.config.imap_host ||
      config.value.smtp_host !== props.account.config.smtp_host),
);
const credentialsNeeded = computed(
  () => identity.value || serversChanged.value,
);
const open = defineModel<boolean>("open", { default: false });
const emit = defineEmits<{ saved: [] }>();
const { t } = useI18n();
const api = useEmailApi();
const defaults = (): EmailConfig => ({
  name: "",
  sender_name: "",
  address: "",
  imap_host: "",
  imap_port: 993,
  smtp_host: "",
  smtp_port: 465,
  folders: ["INBOX"],
  enabled: true,
  ingest: false,
  poll_seconds: 120,
  initial_sync: "new",
});
const config = ref<EmailConfig>(defaults());
const username = ref(""),
  password = ref(""),
  folders = ref("INBOX"),
  smtpPort = ref("465");
const poll = ref("120");
function applyDiscovery(
  imap: EmailEndpoint | undefined,
  smtp: EmailEndpoint | undefined,
) {
  if (loading.value) return;
  if (imap) config.value.imap_host = imap.host;
  if (smtp && (smtp.port === 465 || smtp.port === 587)) {
    config.value.smtp_host = smtp.host;
    config.value.smtp_port = smtp.port;
    smtpPort.value = String(smtp.port);
  }
}
const loading = ref(false),
  error = ref("");
function clear() {
  username.value = "";
  password.value = "";
  error.value = "";
}
watch(
  open,
  (value) => {
    clear();
    if (value) {
      config.value = props.account
        ? {
            ...props.account.config,
            folders: [...props.account.config.folders],
          }
        : defaults();
      folders.value = config.value.folders.join(", ");
      smtpPort.value = String(config.value.smtp_port);
      poll.value = String(config.value.poll_seconds);
    }
  },
  { immediate: true },
);
onDeactivated(() => {
  clear();
  open.value = false;
});
onBeforeUnmount(clear);
async function save() {
  if (loading.value) return;
  loading.value = true;
  error.value = "";
  try {
    if (
      servers.value &&
      ((config.value.imap_host.trim() &&
        !validMailHost(config.value.imap_host)) ||
        !validMailHost(config.value.smtp_host))
    )
      throw new Error("servers");
    if (serversChanged.value && (!username.value || !password.value))
      throw new Error("credentials");
    config.value.folders = folders.value
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
    config.value.poll_seconds = Number(poll.value);
    config.value.smtp_port = parseSmtpPort(Number(smtpPort.value));
    config.value.imap_host = config.value.imap_host.trim();
    if (!config.value.imap_host) config.value.ingest = false;
    if (props.account)
      await api.operation(
        connectionUpdate(
          { ...props.account, config: config.value },
          username.value,
          password.value,
        ),
      );
    else {
      if (!username.value || !password.value) throw new Error("credentials");
      await api.operation({
        operation: "create",
        config: config.value,
        credentials: { username: username.value, password: password.value },
      });
    }
    clear();
    open.value = false;
    emit("saved");
  } catch {
    error.value = t("email.saveError");
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <UiDialog
    v-model:open="open"
    :title="
      !account
        ? $t('email.connect')
        : mode === 'servers'
          ? $t('email.editServers')
          : mode === 'collection'
            ? $t('email.editCollection')
            : $t('email.senderAndCredentials')
    "
    :description="$t('email.credentialsPrivate')"
    :dismissible="!loading"
    :close-label="$t('actions.close')"
  >
    <form
      id="email-connection"
      class="grid gap-4 sm:grid-cols-2"
      @submit.prevent="save"
    >
      <UiAlert v-if="error" tone="danger" class="sm:col-span-2">{{
        error
      }}</UiAlert>
      <EmailDiscovery
        v-if="open && servers && config.imap_host"
        :initial-domain="account?.config.address.split('@').pop() ?? ''"
        :disabled="loading"
        @apply="applyDiscovery"
      />
      <UiInput
        v-if="identity"
        v-model="config.name"
        :label="$t('email.connectionName')"
        required
        maxlength="128"
      />
      <UiInput
        v-if="identity"
        v-model="config.sender_name"
        :label="$t('email.senderName')"
        maxlength="128"
      />
      <UiInput
        v-if="identity"
        v-model="config.address"
        :readonly="!!account"
        type="email"
        :label="$t('email.address')"
        required
        autocomplete="off"
      />
      <UiInput
        v-if="servers"
        v-model="config.imap_host"
        :label="$t('email.imapServer')"
        :help="$t('email.optionalImap')"
        autocomplete="off"
      />
      <UiInput
        v-if="servers"
        v-model="config.smtp_host"
        :label="$t('email.smtpServer')"
        required
        autocomplete="off"
      />
      <UiSelect
        v-if="servers"
        v-model="smtpPort"
        :label="$t('email.smtpSecurity')"
        :options="emailSmtpOptions"
      />
      <UiInput
        v-if="collection"
        v-model="folders"
        :label="$t('email.folders')"
        :help="$t('email.foldersHelp')"
        required
      />
      <UiSelect
        v-if="collection"
        v-model="config.initial_sync"
        :label="$t('email.initialSync')"
        :help="$t('email.initialSyncHelp')"
        :options="[
          { value: 'new', label: $t('email.newOnly') },
          { value: 'all', label: $t('email.allHistory') },
        ]"
      />
      <UiInput
        v-if="collection"
        v-model="poll"
        type="number"
        min="60"
        max="86400"
        :label="$t('email.pollSeconds')"
        required
      />
      <UiInput
        v-if="credentialsNeeded"
        v-model="username"
        :label="
          account ? $t('email.replacementUsername') : $t('email.username')
        "
        autocomplete="off"
        :required="!account || serversChanged"
      />
      <UiInput
        v-if="credentialsNeeded"
        v-model="password"
        type="password"
        :label="
          account ? $t('email.replacementPassword') : $t('email.password')
        "
        autocomplete="new-password"
        :required="!account || serversChanged"
      />
      <UiAlert v-if="credentialsNeeded" tone="info" class="sm:col-span-2">{{
        $t("email.rotationHelp")
      }}</UiAlert>
      <UiCheckbox
        v-if="collection"
        v-model="config.enabled"
        :label="$t('email.enabled')"
      />
      <UiCheckbox
        v-if="collection"
        v-model="config.ingest"
        :label="$t('email.ingest')"
        :help="$t('email.ingestHelp')"
      />
    </form>
    <template #footer
      ><UiButton
        variant="secondary"
        :disabled="loading"
        @click="open = false"
        >{{ $t("actions.cancel") }}</UiButton
      ><UiButton
        type="submit"
        form="email-connection"
        :loading="loading"
        :pending-label="$t('email.saving')"
        >{{ $t("email.saveConnection") }}</UiButton
      ></template
    >
  </UiDialog>
</template>
