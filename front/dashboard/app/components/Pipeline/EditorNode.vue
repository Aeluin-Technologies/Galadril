<script setup lang="ts">
import { Handle, Position } from "@vue-flow/core";
import {
  PencilSquareIcon,
  TrashIcon,
  ArrowsRightLeftIcon,
  CpuChipIcon,
  CircleStackIcon,
  ArrowDownOnSquareIcon,
  BoltIcon,
  AdjustmentsHorizontalIcon,
} from "@heroicons/vue/24/outline";
import { branchLabels } from "~/utils/pipeline";
import type { EditorNodeData } from "~/composables/usePipelineEditor";
const props = defineProps<{ id: string; data: EditorNodeData }>();
const label = computed(() =>
  props.data.stepType === "route"
    ? "pipeline.composition.route"
    : props.data.stepType === "pipeline"
      ? "pipeline.builder.pipelineKind"
      : `pipeline.builder.${props.data.stepType}`,
);
const icon = computed(
  () =>
    ({
      source: CircleStackIcon,
      route: ArrowsRightLeftIcon,
      pipeline: ArrowsRightLeftIcon,
      inference: CpuChipIcon,
      resolve: AdjustmentsHorizontalIcon,
      sink: ArrowDownOnSquareIcon,
      causal: BoltIcon,
      algorithm: CpuChipIcon,
      dbt: CircleStackIcon,
    })[props.data.stepType],
);
defineEmits<{ configure: []; remove: [] }>();
</script>
<template>
  <div
    :data-pipeline-node="id"
    :data-step-kind="data.stepType"
    class="w-64 rounded-xl border border-border bg-surface p-4 text-foreground shadow-sm"
  >
    <Handle
      v-if="data.stepType !== 'source'"
      type="target"
      role="img"
      :position="Position.Left"
      :aria-label="$t('pipeline.composition.input')"
    />
    <div class="mb-2 flex items-center gap-2">
      <component
        :is="icon"
        class="h-4 w-4 shrink-0 text-muted"
        aria-hidden="true"
      />
      <div class="min-w-0 flex-1">
        <p class="break-words font-medium">{{ data.title ?? $t(label) }}</p>
        <p v-if="data.title" class="text-xs text-muted">{{ $t(label) }}</p>
      </div>
      <div class="nodrag ms-auto flex">
        <UiIconButton
          :icon="PencilSquareIcon"
          :label="$t('pipeline.composition.configure')"
          variant="ghost"
          @click="$emit('configure')"
        />
        <UiIconButton
          :icon="TrashIcon"
          :label="$t('ui.actions.delete')"
          variant="ghost"
          @click="$emit('remove')"
        />
      </div>
    </div>
    <p v-if="data.config.model" class="mt-1 break-all text-xs text-muted">
      {{ data.config.model }}
    </p>
    <template v-if="data.stepType === 'route'">
      <div
        v-for="(label, index) in branchLabels(data.config)"
        :key="label"
        class="relative mt-3 pe-2 text-end text-xs text-muted"
      >
        {{ label }}
        <Handle
          :id="label"
          type="source"
          role="img"
          :position="Position.Right"
          :aria-label="label"
          :style="{ top: '50%', right: '-20px' }"
        />
      </div>
    </template>
    <Handle
      v-else-if="data.stepType !== 'pipeline'"
      type="source"
      role="img"
      :position="Position.Right"
      :aria-label="$t('pipeline.composition.output')"
    />
    <PipelineLinkedPreview
      v-if="data.config.pipeline_ref"
      :reference="data.config.pipeline_ref"
    />
  </div>
</template>
