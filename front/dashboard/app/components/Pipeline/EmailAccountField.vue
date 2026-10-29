<script setup lang="ts">
import { can, parseAccounts } from "~/utils/email";
const value = defineModel<string>({ required: true });
const props = defineProps<{ sender: boolean; label: string; help?: string }>();
const api = useEmailApi();
const options = ref<Array<{ value: string; label: string }>>([]);
const failed = ref(false);
const loading = ref(false);
let epoch = 0;
async function load() {
  const own = ++epoch;
  loading.value = true;
  failed.value = false;
  try {
    const accounts = parseAccounts(
      await api.operation({ operation: "accounts" }),
    );
    if (own !== epoch) return;
    options.value = accounts
      .filter(
        (item) =>
          can(item, "use") &&
          (props.sender
            ? can(item, "send") && !!item.config.smtp_host
            : !!item.config.imap_host),
      )
      .map((item) => ({
        value: item.id,
        label: `${item.config.name} · ${item.config.address}`,
      }));
  } catch {
    if (own === epoch) failed.value = true;
  } finally {
    if (own === epoch) loading.value = false;
  }
}
onMounted(load);
onBeforeUnmount(() => {
  epoch++;
  api.cancel();
});
</script>
<template>
  <div class="space-y-2">
    <UiSelect
      v-model="value"
      :label="label"
      :help="help"
      :disabled="loading"
      :options="
        sender
          ? [
              { value: 'source', label: $t('pipeline.email.sameAccount') },
              ...options,
            ]
          : options
      "
    />
    <p v-if="loading" role="status" class="text-xs text-muted">
      {{ $t("email.loading") }}
    </p>
    <UiAlert v-if="failed" tone="danger"
      >{{ $t("email.loadError")
      }}<UiButton variant="ghost" :action="load">{{
        $t("email.retry")
      }}</UiButton></UiAlert
    >
    <NuxtLink
      v-if="!loading && !options.length"
      to="/settings/user#email"
      class="text-sm text-info"
      >{{ $t("pipeline.email.manage") }}</NuxtLink
    >
  </div>
</template>
