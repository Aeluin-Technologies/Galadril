<script setup lang="ts">
import type { Definition, PipelineArtifact } from "~/utils/pipeline";
import type { ReviewConflict } from "~/utils/pipeline-review";
const props = defineProps<{
  artifact: PipelineArtifact;
  definition: Definition;
  branch: string;
}>();
const emit = defineEmits<{
  apply: [artifact: PipelineArtifact, definition: Definition];
}>();
const catalog = usePipelineCatalog();
const result = shallowRef<Awaited<ReturnType<typeof catalog.rebasePreview>>>();
const conflicts = shallowRef<ReviewConflict[]>([]);
const choices = ref<Record<string, "target" | "source">>({});
const busy = ref(false),
  failed = ref(false);
let epoch = 0;
async function preview(reset = true) {
  if (busy.value) return;
  if (reset) choices.value = {};
  busy.value = true;
  failed.value = false;
  const own = ++epoch;
  try {
    const value = await catalog.rebasePreview(
      props.artifact.pipeline_id,
      props.artifact.head_revision_id,
      props.branch,
      props.definition,
      choices.value,
    );
    if (own === epoch) {
      result.value = value;
      if (reset) conflicts.value = value.preview.conflicts;
    }
  } catch {
    if (own === epoch) failed.value = true;
  } finally {
    if (own === epoch) busy.value = false;
  }
}
watch(
  () => props.definition,
  () => {
    result.value = undefined;
  },
  { deep: false },
);
onBeforeUnmount(() => {
  epoch++;
  catalog.cancel();
});
</script>
<template>
  <section
    :aria-label="$t('pipeline.collaboration.rebase')"
    class="space-y-3 rounded-panel border border-border bg-surface p-4"
  >
    <p class="text-sm">{{ $t("pipeline.collaboration.rebaseHelp") }}</p>
    <UiButton variant="secondary" :loading="busy" @click="preview()">{{
      $t("pipeline.collaboration.compareLatest")
    }}</UiButton>
    <UiAlert v-if="failed" tone="danger">{{
      $t("pipeline.collaboration.failed")
    }}</UiAlert>
    <template v-if="result"
      ><PipelineConflictChoices
        v-model="choices"
        :conflicts="conflicts"
        @change="preview(false)"
      />
      <details>
        <summary class="cursor-pointer text-sm">
          {{ $t("pipeline.collaboration.mergedPreview") }}
        </summary>
        <pre
          class="max-h-64 overflow-auto whitespace-pre-wrap break-all text-xs"
          >{{ JSON.stringify(result.preview.definition, null, 2) }}</pre>
      </details>
      <UiButton
        :disabled="busy || result.preview.conflicts.length > 0"
        @click="emit('apply', result.artifact, result.preview.definition)"
        >{{ $t("pipeline.collaboration.applyRebase") }}</UiButton
      ></template
    >
  </section>
</template>
