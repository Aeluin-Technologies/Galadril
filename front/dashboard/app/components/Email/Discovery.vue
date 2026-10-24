<script setup lang="ts">
import type { EmailDiscoveryResult, EmailEndpoint } from "~/utils/email";
import { parseDiscovery } from "~/utils/email";

const props = defineProps<{ initialDomain: string; disabled: boolean }>();
const emit = defineEmits<{
  apply: [imap: EmailEndpoint | undefined, smtp: EmailEndpoint | undefined];
}>();
const { t } = useI18n();
const api = useEmailApi();
const domain = ref(props.initialDomain);
const result = shallowRef<EmailDiscoveryResult>();
const imap = ref(""),
  smtp = ref("");
const loading = ref(false),
  error = ref(""),
  applied = ref(false);
let epoch = 0;
let requested = "";

function invalidate() {
  epoch++;
  api.cancel();
  loading.value = false;
  result.value = undefined;
  error.value = "";
  applied.value = false;
  requested = "";
}
watch(domain, invalidate, { flush: "sync" });
onDeactivated(invalidate);
onBeforeUnmount(invalidate);

async function discover() {
  const value = domain.value.trim();
  if (!value || props.disabled || loading.value || requested === value) return;
  const own = ++epoch;
  loading.value = true;
  error.value = "";
  applied.value = false;
  try {
    const response = parseDiscovery(
      await api.operation({ operation: "discover", domain: value }),
    );
    if (own !== epoch) return;
    result.value = response;
    requested = value;
    imap.value = response.imap[0]?.host ?? "";
    smtp.value = response.smtp[0]
      ? `${response.smtp[0].host}:${response.smtp[0].port}`
      : "";
  } catch {
    if (own === epoch) error.value = t("email.discovery.error");
  } finally {
    if (own === epoch) loading.value = false;
  }
}

function apply() {
  if (!result.value || loading.value || props.disabled) return;
  const incoming = result.value.imap.find((item) => item.host === imap.value);
  const outgoing = result.value.smtp.find(
    (item) => `${item.host}:${item.port}` === smtp.value,
  );
  if (!incoming && !outgoing) return;
  emit("apply", incoming, outgoing);
  applied.value = true;
}
</script>

<template>
  <div
    class="space-y-3 rounded-panel border border-border bg-surface-muted p-4 sm:col-span-2"
  >
    <div class="flex flex-wrap items-end gap-2">
      <UiInput
        v-model="domain"
        :label="$t('email.discovery.domain')"
        :help="$t('email.discovery.help')"
        autocomplete="off"
        :disabled="disabled"
        class="min-w-0 flex-1"
        @blur="discover"
        @keydown.enter.prevent="discover"
      />
      <UiButton
        type="button"
        variant="secondary"
        :loading="loading"
        :disabled="disabled || !domain.trim()"
        :pending-label="$t('email.discovery.loading')"
        @click="
          requested = '';
          discover();
        "
        >{{ $t("email.discovery.detect") }}</UiButton
      >
    </div>
    <UiAlert v-if="error" tone="danger">{{ error }}</UiAlert>
    <div v-if="result" class="space-y-3">
      <p class="text-sm text-muted">{{ $t("email.discovery.review") }}</p>
      <div class="grid gap-3 sm:grid-cols-2">
        <UiSelect
          v-if="result.imap.length"
          v-model="imap"
          :label="$t('email.discovery.imap')"
          :disabled="disabled || loading"
          :options="
            result.imap.map((item) => ({
              value: item.host,
              label: `${item.host} · TLS (${item.port})`,
            }))
          "
        />
        <p v-else role="status" class="text-sm text-muted">
          {{ $t(`email.discovery.imapStatus.${result.imap_status}`) }}
        </p>
        <UiSelect
          v-if="result.smtp.length"
          v-model="smtp"
          :label="$t('email.discovery.smtp')"
          :disabled="disabled || loading"
          :options="
            result.smtp.map((item) => ({
              value: `${item.host}:${item.port}`,
              label: `${item.host} · ${item.security === 'tls' ? 'TLS' : 'STARTTLS'} (${item.port})`,
            }))
          "
        />
        <p v-else role="status" class="text-sm text-muted">
          {{ $t(`email.discovery.smtpStatus.${result.smtp_status}`) }}
        </p>
      </div>
      <UiButton
        v-if="result.imap.length || result.smtp.length"
        type="button"
        variant="secondary"
        :disabled="disabled || loading"
        @click="apply"
        >{{ $t("email.discovery.apply") }}</UiButton
      >
      <p v-if="applied" role="status" class="text-sm text-muted">
        {{ $t("email.discovery.applied") }}
      </p>
    </div>
  </div>
</template>
