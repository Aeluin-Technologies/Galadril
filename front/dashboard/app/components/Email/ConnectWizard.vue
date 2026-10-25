<script setup lang="ts">
import { CheckIcon } from "@heroicons/vue/20/solid";
import type { EmailConfig, EmailDiscoveryResult } from "~/utils/email";
import {
  parseDiscovery,
  mailHost,
  validMailHost,
  parseSmtpPort,
  emailSmtpOptions,
} from "~/utils/email";
const open = defineModel<boolean>("open", { default: false });
const props = defineProps<{ providers: string[] }>();
const emit = defineEmits<{
  saved: [];
  oauth: [provider: string, address: string];
}>();
const { t } = useI18n();
const api = useEmailApi();
const phase = ref<"address" | "servers" | "credentials">("address");
const address = ref(""),
  username = ref(""),
  password = ref("");
const imap = ref(""),
  smtp = ref(""),
  port = ref("465");
const result = shallowRef<EmailDiscoveryResult>();
const busy = ref(false),
  error = ref(""),
  manualPassword = ref(false),
  manualServers = ref(false);
let epoch = 0;
const provider = computed(() =>
  manualServers.value ? undefined : result.value?.oauth_provider,
);
const useOAuth = computed(
  () =>
    !!provider.value &&
    props.providers.includes(provider.value) &&
    !manualPassword.value,
);
const steps = ["address", "servers", "credentials"] as const;
const activeStep = computed(() =>
  phase.value === "address" && busy.value ? 1 : steps.indexOf(phase.value),
);
function reset() {
  epoch++;
  api.cancel();
  phase.value = "address";
  address.value =
    username.value =
    password.value =
    imap.value =
    smtp.value =
    error.value =
      "";
  port.value = "465";
  result.value = undefined;
  manualPassword.value = false;
  manualServers.value = false;
  busy.value = false;
}
watch(open, reset);
onDeactivated(() => {
  open.value = false;
  reset();
});
onBeforeUnmount(reset);
function back() {
  epoch++;
  api.cancel();
  password.value = "";
  error.value = "";
  busy.value = false;
  phase.value =
    phase.value === "credentials" && manualServers.value
      ? "servers"
      : "address";
}
async function discover() {
  const own = ++epoch;
  busy.value = true;
  error.value = "";
  result.value = undefined;
  imap.value = smtp.value = password.value = "";
  manualPassword.value = false;
  manualServers.value = false;
  const domain = address.value.trim().split("@").pop() ?? "";
  try {
    const found = parseDiscovery(
      await api.operation({ operation: "discover", domain }),
    );
    if (own !== epoch) return;
    result.value = found;
    imap.value = found.imap[0]?.host ?? "";
    smtp.value = found.smtp[0]?.host ?? "";
    port.value = String(found.smtp[0]?.port ?? 465);
    username.value =
      found.username === "manual"
        ? ""
        : found.username === "local_part"
          ? (address.value.split("@")[0] ?? "")
          : address.value.trim();
    phase.value = imap.value && smtp.value ? "credentials" : "servers";
  } catch {
    if (own !== epoch) return;
    phase.value = "servers";
    error.value = t("email.discovery.manualFallback");
    username.value = address.value.trim();
  } finally {
    if (own === epoch) busy.value = false;
  }
}
async function next() {
  if (busy.value) return;
  if (phase.value === "address") return discover();
  if (phase.value === "servers") {
    if (
      (imap.value.trim() && !validMailHost(imap.value)) ||
      !validMailHost(smtp.value)
    ) {
      error.value = t("email.wizard.invalidServers");
      return;
    }
    const own = ++epoch;
    busy.value = true;
    error.value = "";
    try {
      await api.operation({
        operation: "validate_servers",
        servers: {
          imap_host: imap.value.trim() ? mailHost(imap.value) : "",
          smtp_host: mailHost(smtp.value),
          smtp_port: Number(port.value),
        },
      });
      if (own !== epoch) return;
      imap.value = imap.value.trim() ? mailHost(imap.value) : "";
      smtp.value = mailHost(smtp.value);
      phase.value = "credentials";
      manualServers.value = true;
      manualPassword.value = true;
    } catch {
      if (own === epoch) error.value = t("email.wizard.serversUnavailable");
    } finally {
      if (own === epoch) busy.value = false;
    }
    return;
  }
  if (useOAuth.value) {
    if (provider.value && props.providers.includes(provider.value))
      emit("oauth", provider.value, address.value.trim());
    return;
  }
  const own = epoch;
  busy.value = true;
  error.value = "";
  const config: EmailConfig = {
    name: address.value.trim(),
    address: address.value.trim(),
    imap_host: imap.value.trim(),
    imap_port: 993,
    smtp_host: smtp.value.trim(),
    smtp_port: parseSmtpPort(Number(port.value)),
    folders: ["INBOX"],
    enabled: true,
    ingest: false,
    poll_seconds: 120,
    initial_sync: "new",
  };
  try {
    await api.operation({
      operation: "create",
      config,
      credentials: {
        username: username.value.trim(),
        password: password.value,
      },
    });
    if (own !== epoch) return;
    open.value = false;
    emit("saved");
  } catch {
    if (own === epoch) error.value = t("email.saveError");
  } finally {
    password.value = "";
    if (own === epoch) busy.value = false;
  }
}
</script>
<template>
  <UiDialog
    v-model:open="open"
    :title="$t('email.connect')"
    :description="
      phase === 'address' ? $t('email.wizard.description') : undefined
    "
    :dismissible="!busy"
    :close-label="$t('actions.close')"
  >
    <form id="email-connect-wizard" class="grid gap-4" @submit.prevent="next">
      <div class="grid gap-3">
        <p class="font-semibold" role="status" aria-live="polite">
          {{
            $t("email.wizard.progress", {
              step: activeStep + 1,
              label: $t(`email.wizard.${steps[activeStep]}`),
            })
          }}
        </p>
        <ol
          class="grid grid-cols-3 gap-2"
          :aria-label="$t('email.wizard.steps')"
        >
          <li
            v-for="(label, index) in steps"
            :key="label"
            :aria-current="activeStep === index ? 'step' : undefined"
            :class="[
              'flex min-w-0 items-center gap-2 rounded-control border px-2 py-3 text-xs',
              activeStep === index
                ? 'border-primary-active bg-primary-subtle font-semibold text-primary-foreground'
                : 'border-border text-muted',
            ]"
          >
            <span
              :class="[
                'flex size-6 shrink-0 items-center justify-center rounded-full',
                index <= activeStep
                  ? 'bg-primary text-on-primary'
                  : 'bg-surface-muted',
              ]"
            >
              <CheckIcon
                v-if="index < activeStep"
                class="size-4"
                aria-hidden="true"
              /><span v-else aria-hidden="true">{{ index + 1 }}</span>
            </span>
            <span>{{ $t(`email.wizard.${label}`) }}</span>
          </li>
        </ol>
      </div>
      <UiAlert v-if="error" tone="warning">{{ error }}</UiAlert>
      <UiInput
        v-if="phase === 'address'"
        v-model="address"
        type="email"
        :label="$t('email.address')"
        :help="$t('email.wizard.addressHelp')"
        autocomplete="email"
        required
        :disabled="busy"
      />
      <template v-else>
        <div class="flex flex-wrap items-center justify-between gap-2">
          <p class="break-all text-sm font-medium">{{ address }}</p>
          <UiButton
            v-if="phase === 'credentials'"
            type="button"
            variant="ghost"
            size="sm"
            @click="
              phase = 'servers';
              password = '';
            "
            >{{ $t("email.wizard.editServers") }}</UiButton
          >
        </div>
        <template v-if="phase === 'servers'">
          <p class="text-sm text-muted">{{ $t("email.wizard.manualHelp") }}</p>
          <UiInput
            v-model="imap"
            :label="$t('email.imapServer')"
            :help="$t('email.optionalImap')"
            :disabled="busy"
            autocomplete="off"
          />
          <UiInput
            v-model="smtp"
            :label="$t('email.smtpServer')"
            :disabled="busy"
            autocomplete="off"
            required
          />
          <UiSelect
            v-model="port"
            :label="$t('email.smtpSecurity')"
            :options="emailSmtpOptions"
          />
        </template>
        <template v-else>
          <UiAlert v-if="!manualServers" tone="success">{{
            $t("email.wizard.detected")
          }}</UiAlert>
          <template v-if="useOAuth">
            <p class="text-sm text-muted">{{ $t("email.wizard.oauthHelp") }}</p>
            <UiButton
              type="button"
              variant="secondary"
              @click="manualPassword = true"
              >{{ $t("email.wizard.usePassword") }}</UiButton
            >
          </template>
          <template v-else>
            <UiInput
              v-model="username"
              :label="$t('email.username')"
              :help="
                result?.username === 'manual'
                  ? $t('email.wizard.usernameHelp')
                  : undefined
              "
              autocomplete="username"
              required
            />
            <UiInput
              v-model="password"
              type="password"
              :label="$t('email.password')"
              :help="
                provider === 'google'
                  ? $t('email.wizard.appPassword')
                  : undefined
              "
              autocomplete="new-password"
              required
            />
          </template>
          <p class="text-sm text-muted">
            {{ $t(imap ? "email.wizard.defaults" : "email.sendingOnlyHelp") }}
          </p>
        </template>
      </template>
    </form>
    <template #footer>
      <UiButton
        v-if="phase !== 'address'"
        type="button"
        variant="secondary"
        :disabled="busy"
        @click="back"
        >{{ $t("email.wizard.back") }}</UiButton
      >
      <UiButton
        v-else
        type="button"
        variant="secondary"
        :disabled="busy"
        @click="open = false"
        >{{ $t("actions.cancel") }}</UiButton
      >
      <UiButton
        type="submit"
        form="email-connect-wizard"
        :loading="busy"
        :pending-label="
          phase === 'address'
            ? $t('email.discovery.loading')
            : $t('email.saving')
        "
        :disabled="
          phase === 'credentials' &&
          useOAuth &&
          !!provider &&
          !providers.includes(provider)
        "
        >{{
          phase !== "credentials"
            ? $t("email.wizard.continue")
            : useOAuth
              ? provider === "google"
                ? $t("email.google")
                : $t("email.microsoft")
              : $t("email.saveConnection")
        }}</UiButton
      >
    </template>
  </UiDialog>
</template>
