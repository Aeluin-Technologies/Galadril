<script setup lang="ts">
import { EyeIcon } from "@heroicons/vue/24/outline";
import type { PipelineArtifact, PipelineReference } from "~/utils/pipeline";
const props = withDefaults(
  defineProps<{
    reference: PipelineReference;
    depth?: number;
    visited?: string[];
  }>(),
  { depth: 0, visited: () => [] },
);
const catalog = usePipelineCatalog();
const open = ref(false),
  failed = ref(false),
  loading = ref(false);
const artifact = shallowRef<PipelineArtifact>();
let generation = 0;
function clear() {
  generation++;
  catalog.cancel();
  artifact.value = undefined;
  loading.value = false;
  failed.value = false;
}
watch(
  () => [props.reference.pipeline_id, props.reference.revision_id],
  () => {
    open.value = false;
    clear();
  },
);
onBeforeUnmount(clear);
onDeactivated(() => {
  open.value = false;
  clear();
});
watch(open, (visible) => {
  if (visible) void load();
  else clear();
});
async function load() {
  const request = ++generation;
  failed.value = false;
  artifact.value = undefined;
  if (props.depth >= 3 || props.visited.includes(props.reference.pipeline_id)) {
    failed.value = true;
    return;
  }
  loading.value = true;
  try {
    const result = await catalog.read(
      props.reference.pipeline_id,
      props.reference.revision_id,
    );
    if (generation === request) {
      artifact.value = result;
    }
  } catch {
    if (generation === request) failed.value = true;
  } finally {
    if (generation === request) loading.value = false;
  }
}
</script>
<template>
  <UiIconButton
    :icon="EyeIcon"
    size="sm"
    :label="$t('pipeline.composition.show')"
    @click.stop="open = true"
  />
  <UiDialog
    v-model:open="open"
    :title="artifact?.name ?? $t('pipeline.composition.show')"
    :description="$t('pipeline.composition.previewHelp')"
  >
    <p v-if="loading" role="status">{{ $t("pipeline.composition.loading") }}</p>
    <UiAlert v-if="failed" tone="danger" class="mt-2">{{
      $t("pipeline.composition.unavailable")
    }}</UiAlert>
    <div
      v-if="artifact"
      class="mt-3 grid max-h-80 gap-2 overflow-y-auto rounded-lg border border-border bg-canvas p-3"
    >
      <p class="font-mono text-xs text-muted" :title="reference.revision_id">
        {{ reference.revision_id.slice(0, 12) }}
      </p>
      <ol
        class="grid gap-2"
        :aria-label="$t('pipeline.composition.linked_blocks')"
      >
        <li
          v-for="step in artifact.definition.pipeline"
          :key="step.step"
          class="rounded border border-border bg-surface p-2 text-sm"
        >
          <span class="block break-words font-medium">{{ step.step }}</span>
          <span class="text-xs text-muted"
            >{{ step.input_from.join(", ") }} → {{ step.type }}</span
          >
          <p
            v-for="(targets, label) in step.branches"
            :key="label"
            class="text-xs"
          >
            {{ label }} → {{ targets.join(", ") }}
          </p>
          <LinkedPreview
            v-if="step.pipeline_ref"
            :reference="step.pipeline_ref"
            :depth="depth + 1"
            :visited="[...visited, reference.pipeline_id]"
          />
        </li>
      </ol>
    </div>
  </UiDialog>
</template>
