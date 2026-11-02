<script setup lang="ts">
import type { PipelineArtifact } from "~/utils/pipeline";
import type { MergePreview, ReviewConflict } from "~/utils/pipeline-review";
const props = defineProps<{ pipelineId: string; proposalId: string }>();
const catalog = usePipelineCatalog();
const { session } = useAccount();
const artifact = shallowRef<PipelineArtifact>(),
  preview = shallowRef<MergePreview>();
const conflicts = shallowRef<ReviewConflict[]>([]),
  choices = ref<Record<string, "target" | "source">>({});
const busy = ref(false),
  failed = ref(false),
  confirming = ref(false);
let epoch = 0;
const proposal = computed(
  () => artifact.value?.authoring?.proposals[props.proposalId],
);
const editorLink = computed(() => ({
  path: `/pipeline/${encodeURIComponent(props.pipelineId)}`,
  query: { branch: proposal.value?.source_branch ?? "main" },
}));
async function load() {
  const request = ++epoch;
  busy.value = true;
  failed.value = false;
  try {
    const current = await catalog.read(props.pipelineId);
    const selected = current.authoring?.proposals[props.proposalId];
    if (!selected) throw new Error("PROPOSAL_UNAVAILABLE");
    const result = await catalog.mergePreview(
      props.pipelineId,
      selected.source_branch,
      choices.value,
    );
    if (request !== epoch) return;
    artifact.value = current;
    preview.value = result;
    if (!Object.keys(choices.value).length) conflicts.value = result.conflicts;
  } catch {
    if (request === epoch) failed.value = true;
  } finally {
    if (request === epoch) busy.value = false;
  }
}
async function merge() {
  if (
    !artifact.value ||
    !preview.value ||
    preview.value.conflicts.length ||
    busy.value
  )
    return;
  const request = ++epoch;
  busy.value = true;
  failed.value = false;
  try {
    await catalog.author(
      props.pipelineId,
      artifact.value.definition,
      {
        operation: "merge",
        proposal_id: props.proposalId,
        resolutions: choices.value,
      },
      artifact.value.head_revision_id,
    );
    if (request === epoch)
      await navigateTo(`/pipeline/${encodeURIComponent(props.pipelineId)}`);
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
  artifact.value = undefined;
  preview.value = undefined;
  choices.value = {};
  conflicts.value = [];
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
        :to="editorLink"
        class="text-sm text-primary-foreground hover:underline"
        >{{ $t("pipeline.review.backToEditor") }}</NuxtLink
      >
      <div class="mt-3 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 class="text-xl font-semibold">
            {{ $t("pipeline.collaboration.review") }}
          </h1>
          <p v-if="proposal" class="mt-1 text-sm text-muted">
            {{ proposal.source_branch }} → main · {{ proposal.author_id }}
          </p>
        </div>
        <UiButton
          :disabled="busy || !preview || !!preview.conflicts.length || failed"
          @click="confirming = true"
          >{{ $t("pipeline.collaboration.merge") }}</UiButton
        >
      </div>
    </header>
    <div class="min-h-0 flex-1 space-y-5 overflow-auto p-6">
      <p v-if="busy" role="status">{{ $t("pipeline.library.loading") }}</p>
      <UiAlert v-if="failed" tone="danger"
        >{{ $t("pipeline.collaboration.failed")
        }}<UiButton
          variant="ghost"
          @click="
            choices = {};
            load();
          "
          >{{ $t("pipeline.library.retry") }}</UiButton
        ></UiAlert
      >
      <template v-if="preview && artifact"
        ><p class="text-sm text-muted">{{ $t("pipeline.review.mergeHelp") }}</p>
        <PipelineConflictChoices
          v-model="choices"
          :conflicts="conflicts"
          @change="load" />
        <LazyPipelineGraphPreview :definition="preview.definition" />
        <PipelineDefinitionDiff
          :before="artifact.definition"
          :after="preview.definition"
      /></template>
    </div>
    <UiDialog
      v-model:open="confirming"
      :title="$t('pipeline.collaboration.merge')"
      :description="$t('pipeline.review.mergeHelp')"
      ><template #footer
        ><UiButton variant="secondary" @click="confirming = false">{{
          $t("pipeline.library.cancel")
        }}</UiButton
        ><UiButton
          :disabled="busy || failed || !preview || !!preview.conflicts.length"
          :loading="busy"
          @click="merge"
          >{{ $t("pipeline.review.confirmMerge") }}</UiButton
        ></template
      ></UiDialog
    >
  </section>
</template>
