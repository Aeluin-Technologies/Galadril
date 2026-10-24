<script setup lang="ts">
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
const expanded = ref(false),
  failed = ref(false),
  loading = ref(false);
const artifact = shallowRef<PipelineArtifact>();
let generation = 0;
function clear() {
  generation++;
  catalog.cancel();
  artifact.value = undefined;
  expanded.value = false;
  failed.value = false;
}
watch(() => [props.reference.pipeline_id, props.reference.revision_id], clear);
onDeactivated(clear);
async function toggle() {
  if (expanded.value) {
    clear();
    return;
  }
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
      expanded.value = true;
    }
  } catch {
    if (generation === request) failed.value = true;
  } finally {
    if (generation === request) loading.value = false;
  }
}
</script>
<template>
  <div class="nodrag nowheel mt-3 min-w-0 border-t border-border pt-3">
    <UiButton
      variant="secondary"
      size="sm"
      :action="toggle"
      :disabled="loading"
      :aria-expanded="expanded"
    >
      {{
        $t(expanded ? "pipeline.composition.hide" : "pipeline.composition.show")
      }}
    </UiButton>
    <UiAlert v-if="failed" tone="danger" class="mt-2">{{
      $t("pipeline.composition.unavailable")
    }}</UiAlert>
    <div
      v-if="expanded && artifact"
      class="mt-3 grid max-h-80 gap-2 overflow-y-auto rounded-lg border border-border bg-canvas p-3"
    >
      <p class="font-medium">{{ artifact.name }}</p>
      <p class="break-all text-xs text-muted">{{ reference.revision_id }}</p>
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
          <PipelineLinkedPreview
            v-if="step.pipeline_ref"
            :reference="step.pipeline_ref"
            :depth="depth + 1"
            :visited="[...visited, reference.pipeline_id]"
          />
        </li>
      </ol>
    </div>
  </div>
</template>
