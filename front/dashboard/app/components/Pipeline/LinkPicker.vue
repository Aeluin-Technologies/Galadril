<script setup lang="ts">
import type { PipelineArtifact, PipelineSummary } from "~/utils/pipeline";
const open = defineModel<boolean>({ required: true });
const emit = defineEmits<{
  select: [artifact: PipelineArtifact, inputSource: string];
}>();
const catalog = usePipelineCatalog();
const targets = ref<PipelineSummary[]>([]);
const selected = ref("");
const inputSource = ref("");
const artifact = shallowRef<PipelineArtifact>();
const loading = ref(false);
const failed = ref(false);
let generation = 0;
async function refresh() {
  const request = ++generation;
  loading.value = true;
  failed.value = false;
  targets.value = [];
  selected.value = "";
  try {
    const result = await catalog.list();
    if (request === generation && open.value) targets.value = result;
  } catch {
    if (request === generation && open.value) failed.value = true;
  } finally {
    if (request === generation) loading.value = false;
  }
}
watch(open, (value) => {
  if (value) void refresh();
  else {
    generation++;
    catalog.cancel();
    artifact.value = undefined;
  }
});
watch(selected, async (value) => {
  const target = targets.value.find((item) => item.pipeline_id === value);
  artifact.value = undefined;
  inputSource.value = "";
  if (!target?.published_revision_id) return;
  catalog.cancel();
  const request = ++generation;
  loading.value = true;
  failed.value = false;
  try {
    const result = await catalog.read(
      target.pipeline_id,
      target.published_revision_id,
    );
    if (request === generation && open.value) {
      artifact.value = result;
      inputSource.value = result.definition.sources[0]?.id ?? "";
    }
  } catch {
    if (request === generation && open.value) failed.value = true;
  } finally {
    if (request === generation) loading.value = false;
  }
});
async function add() {
  const target = targets.value.find(
    (item) => item.pipeline_id === selected.value,
  );
  if (!target?.published_revision_id) return;
  failed.value = false;
  try {
    const result = await catalog.read(
      target.pipeline_id,
      target.published_revision_id,
    );
    if (!open.value || selected.value !== target.pipeline_id) return;
    if (
      !result.definition.sources.some(
        (source) => source.id === inputSource.value,
      )
    )
      throw new Error("PIPELINE_UNAVAILABLE");
    emit("select", result, inputSource.value);
    open.value = false;
  } catch (error) {
    failed.value = true;
    throw error;
  }
}
</script>
<template>
  <UiDialog v-model:open="open" :title="$t('pipeline.composition.choose')">
    <p class="mb-4 text-muted">
      {{ $t("pipeline.composition.visible_targets") }}
    </p>
    <p v-if="loading" role="status">{{ $t("pipeline.composition.loading") }}</p>
    <UiAlert v-if="failed" tone="danger">{{
      $t("pipeline.composition.unavailable")
    }}</UiAlert>
    <p v-else-if="!loading && !targets.length" role="status">
      {{ $t("pipeline.composition.empty") }}
    </p>
    <UiSelect
      v-model="selected"
      :label="$t('pipeline.composition.pipeline')"
      :options="[
        { value: '', label: $t('pipeline.composition.select') },
        ...targets.map((target) => ({
          value: target.pipeline_id,
          label: target.name,
        })),
      ]"
      :disabled="loading"
    />
    <UiSelect
      v-if="artifact"
      v-model="inputSource"
      :label="$t('pipeline.composition.receiving_input')"
      :options="
        artifact.definition.sources.map((source) => ({
          value: source.id,
          label: source.id,
        }))
      "
    />
    <div class="mt-5 flex flex-wrap gap-2">
      <UiButton
        :action="add"
        :disabled="!selected || !inputSource || loading"
        @action-error="failed = true"
        >{{ $t("pipeline.composition.add_pipeline") }}</UiButton
      >
      <UiButton variant="secondary" :action="refresh">{{
        $t("pipeline.composition.refresh")
      }}</UiButton>
    </div>
  </UiDialog>
</template>
