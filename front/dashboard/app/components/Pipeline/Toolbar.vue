<script setup lang="ts">
import {
  PlusIcon,
  Squares2X2Icon,
  CheckCircleIcon,
  ArrowUturnLeftIcon,
  ArrowUturnRightIcon,
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
const props = defineProps<{
  blocks: number;
  transfers: number;
  canUndo: boolean;
  canRedo: boolean;
  checking: boolean;
  validated: boolean;
}>();
const emit = defineEmits<{
  add: [kind: StepKind | "plugin"];
  undo: [];
  redo: [];
  arrange: [];
  failure: [];
}>();
const { t } = useI18n();
const items = computed<MenuItem[]>(() => [
  {
    id: "filter",
    label: t("pipeline.builder.filter"),
    description: t("pipeline.builder.filterHelp"),
    icon: AdjustmentsHorizontalIcon,
  },
  {
    id: "forward",
    label: t("pipeline.builder.forward"),
    description: t("pipeline.inspector.unionHelp"),
    icon: ArrowsRightLeftIcon,
  },
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
    id === "filter" ||
    id === "forward" ||
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
  <div
    class="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-border bg-surface px-4 py-2 sm:px-6"
  >
    <div class="flex items-center gap-2">
      <UiMenu
        :label="$t('pipeline.builder.add')"
        :items="items"
        variant="secondary"
        @select="add"
        ><template #icon><PlusIcon aria-hidden="true" /></template
      ></UiMenu>
      <div class="flex items-center border-s border-border ps-2">
        <UiIconButton
          :icon="ArrowUturnLeftIcon"
          :label="$t('pipeline.composition.undo')"
          :disabled="!canUndo"
          size="sm"
          variant="ghost"
          @click="emit('undo')"
        /><UiIconButton
          :icon="ArrowUturnRightIcon"
          :label="$t('pipeline.composition.redo')"
          :disabled="!canRedo"
          size="sm"
          variant="ghost"
          @click="emit('redo')"
        />
      </div>
      <UiIconButton
        :icon="Squares2X2Icon"
        :label="$t('pipeline.chrome.arrange')"
        variant="ghost"
        size="sm"
        @click="emit('arrange')"
      />
      <span class="hidden text-xs text-muted sm:inline">{{
        $t("pipeline.composition.blocks", { count: blocks })
      }}</span>
    </div>
    <span
      role="status"
      class="flex items-center gap-2 text-xs"
      :class="validated ? 'text-success' : 'text-muted'"
    >
      <CheckCircleIcon v-if="validated" class="h-4 w-4" />
      {{
        $t(
          checking
            ? "pipeline.chrome.checking"
            : validated
              ? "pipeline.chrome.checked"
              : "pipeline.chrome.autoCheck",
        )
      }}
    </span>
  </div>
</template>
