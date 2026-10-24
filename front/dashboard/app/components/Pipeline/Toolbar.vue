<script setup lang="ts">
import {
  PlusIcon,
  ArrowUturnLeftIcon,
  ArrowUturnRightIcon,
  InformationCircleIcon,
  ArrowDownTrayIcon,
  CircleStackIcon,
  CpuChipIcon,
  ArrowsRightLeftIcon,
  ArrowDownOnSquareIcon,
  BoltIcon,
  PuzzlePieceIcon,
  AdjustmentsHorizontalIcon,
} from "@heroicons/vue/24/outline";
import type { MenuItem } from "@galadril/ui/types";
import type { StepKind } from "~/utils/pipeline";
const name = defineModel<string>({ required: true });
const props = defineProps<{
  blocks: number;
  transfers: number;
  canUndo: boolean;
  canRedo: boolean;
  validate: () => Promise<void>;
  download: () => Promise<void>;
}>();
const emit = defineEmits<{
  add: [kind: StepKind | "plugin"];
  undo: [];
  redo: [];
  failure: [];
}>();
const { t } = useI18n();
const items = computed<MenuItem[]>(() => [
  {
    id: "source",
    label: t("pipeline.builder.source"),
    description: t("pipeline.builder.sourceHelp"),
    icon: CircleStackIcon,
  },
  {
    id: "route",
    label: t("pipeline.composition.route"),
    description: t("pipeline.builder.routeHelp"),
    icon: ArrowsRightLeftIcon,
  },
  {
    id: "inference",
    label: t("pipeline.builder.inference"),
    description: t("pipeline.builder.inferenceHelp"),
    icon: CpuChipIcon,
  },
  {
    id: "resolve",
    label: t("pipeline.builder.resolve"),
    description: t("pipeline.builder.resolveHelp"),
    icon: AdjustmentsHorizontalIcon,
  },
  {
    id: "sink",
    label: t("pipeline.builder.sink"),
    description: t("pipeline.builder.sinkHelp"),
    icon: ArrowDownOnSquareIcon,
  },
  {
    id: "causal",
    label: t("pipeline.builder.causal"),
    description: t("pipeline.builder.causalHelp"),
    icon: BoltIcon,
  },
  {
    id: "plugin",
    label: t("pipeline.builder.plugin"),
    description: t("pipeline.builder.pluginHelp"),
    icon: PuzzlePieceIcon,
  },
  {
    id: "pipeline",
    label: t("pipeline.builder.pipeline"),
    description: t("pipeline.builder.pipelineHelp"),
    icon: ArrowsRightLeftIcon,
    disabled: props.transfers >= 3,
  },
]);
function add(id: string) {
  if (
    id === "source" ||
    id === "route" ||
    id === "inference" ||
    id === "resolve" ||
    id === "sink" ||
    id === "causal" ||
    id === "pipeline" ||
    id === "plugin"
  )
    emit("add", id);
}
</script>
<template>
  <header
    class="grid min-w-0 gap-3 rounded-panel border border-border bg-surface p-4"
  >
    <div class="flex min-w-0 flex-wrap items-end justify-between gap-4">
      <UiInput
        v-model="name"
        :label="$t('pipeline.header.pipeline_name')"
        class="w-full sm:max-w-sm"
      />
      <div class="flex flex-wrap items-center gap-2">
        <UiMenu :label="$t('pipeline.builder.add')" :items="items" @select="add"
          ><template #icon><PlusIcon aria-hidden="true" /></template
        ></UiMenu>
        <UiButton
          variant="secondary"
          :action="validate"
          @action-error="emit('failure')"
          >{{ $t("pipeline.composition.validate") }}</UiButton
        >
        <UiIconButton
          :icon="ArrowDownTrayIcon"
          :label="$t('pipeline.composition.export')"
          :action="download"
          @action-error="emit('failure')"
        />
      </div>
    </div>
    <div
      class="flex min-w-0 flex-wrap items-center justify-between gap-2 border-t border-border pt-2"
    >
      <div class="flex flex-wrap items-center gap-2 text-xs text-muted">
        <span>{{ $t("pipeline.composition.blocks", { count: blocks }) }}</span>
        <span aria-hidden="true">·</span>
        <span>{{
          $t("pipeline.builder.transfers", { count: transfers })
        }}</span>
        <UiIconButton
          :icon="InformationCircleIcon"
          :label="$t('pipeline.composition.limits')"
          size="sm"
        />
      </div>
      <div class="flex items-center gap-1">
        <UiIconButton
          :icon="ArrowUturnLeftIcon"
          :label="$t('pipeline.composition.undo')"
          :disabled="!canUndo"
          size="sm"
          @click="emit('undo')"
        />
        <UiIconButton
          :icon="ArrowUturnRightIcon"
          :label="$t('pipeline.composition.redo')"
          :disabled="!canRedo"
          size="sm"
          @click="emit('redo')"
        />
      </div>
    </div>
  </header>
</template>
