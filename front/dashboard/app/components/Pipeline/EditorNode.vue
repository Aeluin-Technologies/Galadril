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
  ExclamationCircleIcon,
} from "@heroicons/vue/24/outline";
import { branchLabels } from "~/utils/pipeline";
import { readConditions } from "~/utils/pipeline-conditions";
import { nodeCategory, pipelinePalette } from "~/utils/pipeline-visuals";
import type { EditorNodeData } from "~/composables/usePipelineEditor";
const props = defineProps<{
  id: string;
  data: EditorNodeData;
  selected?: boolean;
  issues?: readonly import("~/utils/pipeline").PipelineDiagnostic[];
}>();
const { t } = useI18n();
const label = computed(() =>
  props.data.stepType === "route"
    ? "pipeline.composition.route"
    : props.data.stepType === "pipeline"
      ? "pipeline.builder.pipelineKind"
      : `pipeline.builder.${props.data.stepType}`,
);
const title = computed(() => props.data.title ?? t(label.value));
const category = computed(() => nodeCategory(props.data.stepType));
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
      filter: AdjustmentsHorizontalIcon,
      forward: ArrowsRightLeftIcon,
    })[props.data.stepType],
);
const summary = computed(() => {
  const conditions = readConditions(props.data.config.params?.conditions);
  if (conditions.length)
    return conditions
      .map(
        (rule) =>
          `${t(`pipeline.conditions.fields.${rule.field}`)} ${t(`pipeline.conditions.operators.${rule.operator}`)} ${rule.value || "…"}`,
      )
      .join(` ${t("pipeline.chrome.and")} `);
  if (props.data.stepType === "source")
    return t(
      props.data.config.source_kind === "email"
        ? "pipeline.chrome.emailInput"
        : "pipeline.chrome.recordInput",
    );
  if (props.data.stepType === "forward")
    return t("pipeline.chrome.unionSummary");
  return t(label.value);
});
defineEmits<{ configure: []; remove: [] }>();
</script>
<template>
  <div
    :data-pipeline-node="id"
    :data-step-kind="data.stepType"
    class="relative w-60 border bg-surface text-foreground shadow-sm"
    :class="
      issues?.length
        ? 'border-danger ring-1 ring-danger'
        : selected
          ? 'border-focus ring-2 ring-focus'
          : 'border-border-control'
    "
  >
    <Handle
      v-if="data.stepType !== 'source'"
      id="record"
      type="target"
      role="img"
      :position="Position.Left"
      :aria-label="$t('pipeline.composition.input')"
    />
    <div
      class="flex items-center gap-2 px-3 py-2 text-white"
      :style="{ backgroundColor: pipelinePalette[category].color }"
    >
      <component :is="icon" class="h-4 w-4 shrink-0" aria-hidden="true" />
      <p class="min-w-0 break-words text-sm font-semibold">{{ title }}</p>
    </div>
    <div class="flex items-start gap-1 px-3 py-2">
      <p class="min-w-0 flex-1 break-words text-xs leading-5 text-muted">
        {{ summary }}
      </p>
      <div class="nodrag flex shrink-0">
        <UiIconButton
          :icon="PencilSquareIcon"
          :label="$t('pipeline.composition.configure')"
          variant="ghost"
          size="sm"
          @click.stop="$emit('configure')"
        /><UiIconButton
          :icon="TrashIcon"
          :label="$t('ui.actions.delete')"
          variant="ghost"
          size="sm"
          @click.stop="$emit('remove')"
        />
      </div>
    </div>
    <p
      v-if="
        data.stepType === 'route' &&
        data.config.routing === 'slm' &&
        data.config.model
      "
      class="px-3 pb-2 text-xs text-muted"
    >
      {{ data.config.model }}
    </p>
    <p
      v-if="issues?.length"
      class="flex items-center gap-1 border-t border-danger/20 bg-danger-subtle px-3 py-1.5 text-xs text-danger"
    >
      <ExclamationCircleIcon class="h-4 w-4 shrink-0" aria-hidden="true" />{{
        $t(`pipeline.diagnostics.codes.${issues[0]?.code ?? "field"}`)
      }}
    </p>
    <template v-if="data.stepType === 'route'"
      ><div
        v-for="branch in branchLabels(data.config)"
        :key="branch"
        class="relative border-t border-border px-3 py-1 text-end text-xs text-muted"
      >
        {{ branch
        }}<Handle
          :id="branch"
          type="source"
          role="img"
          :position="Position.Right"
          :aria-label="branch"
        /></div
    ></template>
    <Handle
      v-else-if="data.stepType !== 'pipeline'"
      id="record"
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
