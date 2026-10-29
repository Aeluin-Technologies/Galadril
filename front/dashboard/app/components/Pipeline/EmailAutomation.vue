<script setup lang="ts">
import { can, parseAccounts } from "~/utils/email";
import type { EmailAccount } from "~/utils/email";
import type { PipelineArtifact } from "~/utils/pipeline";
const props = defineProps<{ artifact: PipelineArtifact }>();
const api = useEmailApi();
const catalog = usePipelineCatalog();
const published = shallowRef<PipelineArtifact>();
const accounts = shallowRef<EmailAccount[]>([]);
const selected = ref("");
const loading = ref(false),
  failed = ref(false);
const current = computed(() =>
  accounts.value.find((item) => item.id === selected.value),
);
const compatible = computed(
  () =>
    (published.value?.definition ?? props.artifact.definition).sources.some(
      (item) => item.capability_id === "utils.input.email",
    ) &&
    (published.value?.definition ?? props.artifact.definition).pipeline.some(
      (item) => item.capability_id === "utils.output.reply",
    ),
);
const deployed = computed(() => props.artifact.published_revision_id);
let epoch = 0;
async function load() {
  const own = ++epoch;
  loading.value = true;
  failed.value = false;
  try {
    const [result, pinned] = await Promise.all([
      api.operation({ operation: "accounts" }),
      deployed.value
        ? catalog.read(props.artifact.pipeline_id, deployed.value)
        : Promise.resolve(undefined),
    ]);
    if (own === epoch) {
      published.value = pinned;
      accounts.value = parseAccounts(result).filter(
        (item) => can(item, "use") && can(item, "edit"),
      );
    }
  } catch {
    if (own === epoch) failed.value = true;
  } finally {
    if (own === epoch) loading.value = false;
  }
}
watch(deployed, load, { immediate: true });
onBeforeUnmount(() => {
  epoch++;
  api.cancel();
  catalog.cancel();
});
</script>
<template>
  <section
    :aria-label="$t('pipeline.email.title')"
    class="space-y-4 rounded-panel border border-border bg-surface p-4"
  >
    <h2 class="font-medium">{{ $t("pipeline.email.title") }}</h2>
    <p class="text-sm text-muted">{{ $t("pipeline.email.help") }}</p>
    <NuxtLink to="/settings/user#email" class="text-sm text-info">{{
      $t("pipeline.email.manage")
    }}</NuxtLink>
    <p v-if="loading" role="status">{{ $t("pipeline.library.loading") }}</p>
    <UiAlert v-if="failed" tone="danger"
      >{{ $t("email.loadError")
      }}<UiButton variant="ghost" :action="load">{{
        $t("email.retry")
      }}</UiButton></UiAlert
    >
    <UiAlert v-if="!compatible" tone="info">{{
      $t("pipeline.email.incompatible")
    }}</UiAlert
    ><UiAlert v-else-if="!deployed" tone="info">{{
      $t("pipeline.email.notDeployed")
    }}</UiAlert>
    <template v-else
      ><p v-if="!loading && !accounts.length" class="text-sm text-muted">
        {{ $t("pipeline.email.noAccounts") }}
      </p>
      <UiSelect
        v-if="accounts.length"
        v-model="selected"
        :label="$t('pipeline.email.account')"
        :options="
          accounts.map((item) => ({ value: item.id, label: item.config.name }))
        " /><EmailProfile
        v-if="current"
        :key="current.id + deployed"
        :account="current"
        :pipeline-binding="{
          pipeline_id: artifact.pipeline_id,
          revision_id: deployed,
        }"
    /></template>
  </section>
</template>
