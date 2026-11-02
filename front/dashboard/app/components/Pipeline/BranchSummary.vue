<script setup lang="ts">
import type { PipelineArtifact } from "~/utils/pipeline";
const props = defineProps<{
  pipelineId: string;
  revision: string;
  names: string[];
}>();
const catalog = usePipelineCatalog();
const artifact = shallowRef<PipelineArtifact>();
const open = ref(false),
  busy = ref(false),
  failed = ref(false);
let epoch = 0;
async function load() {
  if (busy.value || artifact.value) return;
  const request = ++epoch;
  busy.value = true;
  failed.value = false;
  try {
    const value = await catalog.read(props.pipelineId);
    if (request === epoch) artifact.value = value;
  } catch {
    if (request === epoch) failed.value = true;
  } finally {
    if (request === epoch) busy.value = false;
  }
}
function toggle(event: Event) {
  open.value = event.target instanceof HTMLDetailsElement && event.target.open;
  if (open.value) void load();
}
watch(
  () => props.revision,
  () => {
    epoch++;
    catalog.cancel();
    artifact.value = undefined;
    busy.value = false;
    if (open.value) void load();
  },
);
onBeforeUnmount(() => {
  epoch++;
  catalog.cancel();
});
</script>
<template>
  <details v-if="names.some((name) => name !== 'main')" @toggle="toggle">
    <summary class="cursor-pointer text-sm text-primary-foreground">
      {{ $t("pipeline.review.branchCount", { count: names.length }) }}
    </summary>
    <div class="mt-2">
      <p v-if="busy" role="status" class="text-xs text-muted">
        {{ $t("pipeline.library.loading") }}
      </p>
      <UiButton v-if="failed" variant="ghost" size="sm" @click="load">{{
        $t("pipeline.library.retry")
      }}</UiButton
      ><PipelineBranchTree
        v-if="artifact"
        :branches="artifact.authoring?.branches ?? {}"
        selected="main"
        :pipeline-id="pipelineId"
      />
    </div>
  </details>
  <NuxtLink
    v-else
    :to="`/pipeline/${encodeURIComponent(pipelineId)}`"
    class="text-primary-foreground hover:underline"
    >⑂ main</NuxtLink
  >
</template>
