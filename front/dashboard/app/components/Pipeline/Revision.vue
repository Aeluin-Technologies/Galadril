<script setup lang="ts">
import type { Definition, PipelineArtifact } from "~/utils/pipeline";
const props = defineProps<{
  pipelineId: string;
  revisionId: string;
  branch: string;
}>();
const catalog = usePipelineCatalog();
const { session } = useAccount();
const current = shallowRef<PipelineArtifact>(),
  previous = shallowRef<Definition>();
const busy = ref(false),
  failed = ref(false),
  confirming = ref(false);
let epoch = 0;
function definition(value: PipelineArtifact) {
  return props.branch === "main"
    ? value.definition
    : value.authoring?.branches[props.branch]?.definition;
}
const latest = computed(() =>
  current.value ? definition(current.value) : undefined,
);
async function load() {
  const request = ++epoch;
  busy.value = true;
  failed.value = false;
  try {
    const [head, old] = await Promise.all([
      catalog.read(props.pipelineId),
      catalog.read(props.pipelineId, props.revisionId),
    ]);
    if (request !== epoch) return;
    const selected = definition(old);
    if (!selected || !definition(head))
      throw new Error("BRANCH_UNAVAILABLE_AT_REVISION");
    current.value = head;
    previous.value = selected;
  } catch {
    if (request === epoch) failed.value = true;
  } finally {
    if (request === epoch) busy.value = false;
  }
}
async function restore() {
  if (!current.value || !previous.value || busy.value) return;
  const request = ++epoch;
  busy.value = true;
  failed.value = false;
  try {
    await catalog.save(
      props.pipelineId,
      previous.value,
      current.value.head_revision_id,
      "Restore reviewed revision",
      props.branch,
    );
    if (request === epoch)
      await navigateTo({
        path: `/pipeline/${encodeURIComponent(props.pipelineId)}`,
        query: { branch: props.branch },
      });
  } catch {
    if (request === epoch) {
      failed.value = true;
      confirming.value = false;
    }
  } finally {
    if (request === epoch) busy.value = false;
  }
}
function clear() {
  epoch++;
  catalog.cancel();
  current.value = undefined;
  previous.value = undefined;
  busy.value = false;
  confirming.value = false;
}
onActivated(() => {
  void load();
});
onDeactivated(clear);
watch([() => session.value?.tenant, () => session.value?.subject], clear);
onMounted(() => window.addEventListener("pengolo-access-invalidated", clear));
onBeforeUnmount(() => {
  clear();
  window.removeEventListener("pengolo-access-invalidated", clear);
});
</script>
<template>
  <section class="flex h-full min-h-0 flex-col overflow-hidden bg-canvas">
    <header class="shrink-0 border-b border-border bg-surface px-6 py-4">
      <NuxtLink
        :to="{
          path: `/pipeline/${encodeURIComponent(pipelineId)}`,
          query: { branch },
        }"
        class="text-sm text-primary-foreground hover:underline"
        >{{ $t("pipeline.review.backToEditor") }}</NuxtLink
      >
      <div class="mt-3 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 class="text-xl font-semibold">
            {{ $t("pipeline.review.revisionTitle") }}
          </h1>
          <p class="mt-1 font-mono text-xs text-muted">
            {{ revisionId.slice(0, 12) }} · {{ branch }}
          </p>
        </div>
        <UiButton
          variant="secondary"
          :disabled="busy || failed || !previous"
          @click="confirming = true"
          >{{ $t("pipeline.collaboration.restore") }}</UiButton
        >
      </div>
    </header>
    <div class="min-h-0 flex-1 space-y-4 overflow-auto p-6">
      <p v-if="busy" role="status">{{ $t("pipeline.library.loading") }}</p>
      <UiAlert v-if="failed" tone="danger"
        >{{ $t("pipeline.collaboration.failed")
        }}<UiButton variant="ghost" @click="load">{{
          $t("pipeline.library.retry")
        }}</UiButton></UiAlert
      ><template v-if="previous && latest"
        ><p class="text-sm text-muted">
          {{ $t("pipeline.review.revisionHelp") }}
        </p>
        <LazyPipelineGraphPreview :definition="latest" :before="previous" />
        <PipelineDefinitionDiff :before="previous" :after="latest"
      /></template>
    </div>
    <UiDialog
      v-model:open="confirming"
      :title="$t('pipeline.collaboration.restore')"
      :description="$t('pipeline.review.restoreHelp')"
      ><template #footer
        ><UiButton variant="secondary" @click="confirming = false">{{
          $t("pipeline.library.cancel")
        }}</UiButton
        ><UiButton
          :disabled="busy || failed"
          :loading="busy"
          @click="restore"
          >{{ $t("pipeline.review.confirmRestore") }}</UiButton
        ></template
      ></UiDialog
    >
  </section>
</template>
