<script setup lang="ts">
import type { PluginBlock } from "~/utils/plugin-blocks";
import { VueFlow, type NodeProps } from "@vue-flow/core";
import {
  MagnifyingGlassPlusIcon,
  MagnifyingGlassMinusIcon,
  ArrowsPointingOutIcon,
} from "@heroicons/vue/24/outline";
import { Background } from "@vue-flow/background";
import { Controls } from "@vue-flow/controls";
import "@vue-flow/core/dist/style.css";
import "@vue-flow/core/dist/theme-default.css";
import "@vue-flow/controls/dist/style.css";
import { usePipelineEditor } from "~/composables/usePipelineEditor";
import type { EditorNodeData } from "~/composables/usePipelineEditor";
import type { NodeConfig, PipelineArtifact, StepKind } from "~/utils/pipeline";
const { t, locale } = useI18n();
useHead(() => ({ htmlAttrs: { lang: locale.value } }));
const editor = usePipelineEditor();
const { flow, canvas } = usePipelineViewport();
const {
  name,
  nodes,
  edges,
  failure,
  validated,
  expandedBlocks,
  canUndo,
  canRedo,
} = editor;
const kind = ref<StepKind>("route");
const pluginsOpen = ref(false);
function pluginSelected(plugin: PluginBlock) {
  try {
    editor.addPlugin(plugin);
  } catch {
    failure.value = true;
  }
}
const linking = ref(false),
  configuring = ref(false),
  selected = ref<string>();
const current = computed(() =>
  nodes.value.find((node) => node.id === selected.value),
);
const incoming = computed(() =>
  edges.value.find((edge) => edge.target === selected.value),
);
const options: Array<{ value: StepKind; label: string }> = [
  { value: "source", label: t("pipeline.modules.source") },
  { value: "route", label: t("pipeline.composition.route") },
  { value: "inference", label: t("pipeline.modules.inference") },
  { value: "resolve", label: t("pipeline.modules.resolve") },
  { value: "sink", label: t("pipeline.modules.sink") },
  { value: "causal", label: t("pipeline.modules.causal") },
];
function add() {
  try {
    editor.add(kind.value);
  } catch {
    failure.value = true;
  }
}
function linked(artifact: PipelineArtifact, inputSource: string) {
  try {
    editor.add("pipeline", artifact, inputSource);
  } catch {
    failure.value = true;
  }
}
function configure(id: string) {
  selected.value = id;
  configuring.value = true;
}
function save(config: NodeConfig, upstream: string, branch: string) {
  if (!selected.value) return;
  try {
    editor.configure(selected.value, config, upstream, branch);
    configuring.value = false;
  } catch {
    failure.value = true;
  }
}
function keyboard(event: KeyboardEvent) {
  if (
    event.target instanceof HTMLElement &&
    event.target.closest(
      "input, select, textarea, [contenteditable=true], [role=combobox]",
    )
  )
    return;
  const modifier = event.ctrlKey || event.metaKey;
  const key = event.key.toLowerCase();
  if (modifier && (key === "z" || key === "y")) {
    event.preventDefault();
    if (key === "y" || event.shiftKey) editor.redo();
    else editor.undo();
  } else if (modifier && key === "a") {
    event.preventDefault();
    flow.addSelectedNodes(flow.getNodes.value);
    flow.addSelectedEdges(flow.getEdges.value);
  } else if (event.key === "Delete" || event.key === "Backspace") {
    event.preventDefault();
    const selectedNodes = new Set(
      flow.getNodes.value
        .filter((node) => node.selected)
        .map((node) => node.id),
    );
    const selectedEdges = new Set(
      flow.getEdges.value
        .filter((edge) => edge.selected)
        .map((edge) => edge.id),
    );
    if (!selectedNodes.size && !selectedEdges.size) return;
    nodes.value = nodes.value.filter((node) => !selectedNodes.has(node.id));
    edges.value = edges.value.filter(
      (edge) =>
        !selectedNodes.has(edge.source) &&
        !selectedNodes.has(edge.target) &&
        !selectedEdges.has(edge.id),
    );
    editor.remember();
  }
}
async function validate() {
  await editor.validate();
}
</script>
<template>
  <section
    class="grid min-w-0 gap-4 p-4 md:p-6"
    :aria-label="$t('pipeline.composition.editor')"
    @keydown="keyboard"
  >
    <header class="grid min-w-0 gap-4">
      <UiInput v-model="name" :label="$t('pipeline.header.pipeline_name')" />
      <p class="text-sm text-muted">{{ $t("pipeline.composition.limits") }}</p>
      <div class="flex flex-wrap items-end gap-2">
        <UiSelect
          v-model="kind"
          :label="$t('pipeline.composition.block_type')"
          :options="options"
        />
        <UiButton variant="secondary" @click="add">{{
          $t("pipeline.composition.add_block")
        }}</UiButton>
        <UiButton variant="secondary" @click="pluginsOpen = true">{{
          $t("pipeline.plugins.add")
        }}</UiButton>
        <UiButton
          :disabled="
            nodes.filter((node) => node.data.stepType === 'pipeline').length >=
            3
          "
          @click="linking = true"
          >{{ $t("pipeline.composition.link") }}</UiButton
        >
        <UiButton
          variant="secondary"
          :action="validate"
          @action-error="failure = true"
          >{{ $t("pipeline.composition.validate") }}</UiButton
        >
        <UiButton
          variant="secondary"
          :action="editor.download"
          @action-error="failure = true"
          >{{ $t("pipeline.composition.export") }}</UiButton
        >
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <UiButton variant="ghost" :disabled="!canUndo" @click="editor.undo">{{
          $t("pipeline.composition.undo")
        }}</UiButton>
        <UiButton variant="ghost" :disabled="!canRedo" @click="editor.redo">{{
          $t("pipeline.composition.redo")
        }}</UiButton>
        <span class="text-sm text-muted">{{
          $t("pipeline.composition.blocks", { count: nodes.length })
        }}</span>
      </div>
    </header>
    <UiAlert v-if="failure" tone="danger">{{
      $t("pipeline.composition.invalid")
    }}</UiAlert>
    <UiAlert v-if="validated" tone="success">{{
      $t("pipeline.composition.validated", { count: expandedBlocks })
    }}</UiAlert>
    <div
      ref="canvas"
      class="h-[650px] min-w-0 overflow-hidden rounded-xl border border-border bg-canvas"
      role="region"
      :aria-label="$t('pipeline.composition.canvas')"
    >
      <VueFlow
        id="pipeline-editor"
        v-model:nodes="nodes"
        v-model:edges="edges"
        :fit-view-on-init="true"
        :min-zoom="0.1"
        @nodes-initialized="flow.fitView({ padding: 0.2 })"
        :delete-key-code="null"
        @connect="editor.connect"
        @node-drag-stop="editor.remember"
        @edges-change="validated = false"
      >
        <Background :gap="20" />
        <Controls
          :show-zoom="false"
          :show-fit-view="false"
          :show-interactive="false"
        >
          <UiIconButton
            :icon="MagnifyingGlassPlusIcon"
            :label="$t('pipeline.composition.zoom_in')"
            variant="ghost"
            @click="flow.zoomIn()"
          />
          <UiIconButton
            :icon="MagnifyingGlassMinusIcon"
            :label="$t('pipeline.composition.zoom_out')"
            variant="ghost"
            @click="flow.zoomOut()"
          />
          <UiIconButton
            :icon="ArrowsPointingOutIcon"
            :label="$t('pipeline.composition.fit')"
            variant="ghost"
            @click="flow.fitView({ padding: 0.2 })"
          />
        </Controls>
        <template #node-custom="node: NodeProps<EditorNodeData>">
          <PipelineEditorNode
            :id="node.id"
            :data="node.data"
            @configure="configure(node.id)"
            @remove="editor.remove(node.id)"
          />
        </template>
      </VueFlow>
    </div>
    <PipelinePluginPicker v-model="pluginsOpen" @select="pluginSelected" />
    <PipelineLinkPicker v-model="linking" @select="linked" />
    <PipelineBlockSettings
      v-if="configuring && current"
      :key="current.id"
      v-model="configuring"
      :node="current"
      :failed="failure"
      :nodes="nodes"
      :incoming="incoming?.source ?? ''"
      :branch="incoming?.sourceHandle ?? ''"
      @save="save"
    />
  </section>
</template>
