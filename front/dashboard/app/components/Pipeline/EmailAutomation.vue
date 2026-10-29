<script setup lang="ts">
import { parseAccounts } from "~/utils/email";
import type { EmailAccount } from "~/utils/email";
import type { PipelineArtifact } from "~/utils/pipeline";
const props = defineProps<{ artifact: PipelineArtifact }>();
const accounts = shallowRef<EmailAccount[]>([]);
const failed = ref(false);
const api = useEmailApi();
const sources = computed(() =>
  props.artifact.definition.sources.filter(
    (item) => item.capability_id === "utils.input.email",
  ),
);
const outputs = computed(() =>
  props.artifact.definition.pipeline.filter(
    (item) => item.capability_id === "utils.output.reply",
  ),
);
const { t } = useI18n();
function address(id: unknown): string {
  return (
    accounts.value.find((item) => item.id === id)?.config.address ??
    t("pipeline.email.chooseAccount")
  );
}
async function load() {
  failed.value = false;
  try {
    accounts.value = parseAccounts(
      await api.operation({ operation: "accounts" }),
    );
  } catch {
    failed.value = true;
  }
}
onMounted(load);
onBeforeUnmount(() => api.cancel());
</script>
<template>
  <section
    class="border-b border-border bg-surface px-4 py-3 text-sm"
    :aria-label="$t('pipeline.email.title')"
  >
    <h2 class="font-medium">{{ $t("pipeline.email.title") }}</h2>
    <p class="mt-1 text-muted">{{ $t("pipeline.email.executionHelp") }}</p>
    <p v-for="source in sources" :key="source.id" class="mt-2">
      {{ $t("pipeline.email.watch") }}
      <strong>{{ address(source.account_id) }}</strong>
      <span v-for="output in outputs" :key="output.step">
        · {{ $t("pipeline.email.sendFrom") }}
        <strong>{{
          address(
            output.params?.sender_account_id === "source"
              ? source.account_id
              : output.params?.sender_account_id,
          )
        }}</strong>
        ·
        {{
          output.params?.delivery_mode === "automatic"
            ? $t("email.modeAutomatic")
            : output.params?.delivery_mode === "approval"
              ? $t("email.modeApproval")
              : $t("email.modeDraft")
        }}
      </span>
    </p>
    <p v-if="!artifact.published_revision_id" class="mt-1 text-muted">
      {{ $t("pipeline.email.notDeployed") }}
    </p>
    <UiAlert v-if="failed" tone="danger"
      >{{ $t("email.loadError")
      }}<UiButton variant="ghost" :action="load">{{
        $t("email.retry")
      }}</UiButton></UiAlert
    >
  </section>
</template>
