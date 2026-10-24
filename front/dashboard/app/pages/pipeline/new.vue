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
const { locale } = useI18n();
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
function add(kind: StepKind | "plugin") {
  if (kind === "plugin") {
    pluginsOpen.value = true;
    return;
  }
  if (kind === "pipeline") {
    linking.value = true;
    return;
  }
  try {
    editor.add(kind);
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
    <PipelineToolbar
      v-model="name"
      :blocks="nodes.length"
      :transfers="
        nodes.filter((node) => node.data.stepType === 'pipeline').length
      "
      :can-undo="canUndo"
      :can-redo="canRedo"
      :validate="validate"
      :download="editor.download"
      @add="add"
      @undo="editor.undo"
      @redo="editor.redo"
      @failure="failure = true"
    />
    <p class="text-sm text-muted">{{ $t("pipeline.builder.help") }}</p>
    <UiAlert v-if="failure" tone="danger">{{
      $t("pipeline.composition.invalid")
    }}</UiAlert>
    <UiAlert v-if="validated" tone="success">{{
      $t("pipeline.composition.validated", { count: expandedBlocks })
    }}</UiAlert>
    <div
      ref="canvas"
      class="h-[68dvh] min-h-[480px] min-w-0 overflow-hidden rounded-xl border border-border bg-canvas"
      role="region"
      :aria-label="$t('pipeline.composition.canvas')"
    >
      <VueFlow
        id="pipeline-editor"
        v-model:nodes="nodes"
        v-model:edges="edges"
        :fit-view-on-init="true"
        :fit-view-params="{ padding: 0.2, maxZoom: 1 }"
        :min-zoom="0.1"
        @nodes-initialized="flow.fitView({ padding: 0.2, maxZoom: 1 })"
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
            @click="flow.fitView({ padding: 0.2, maxZoom: 1 })"
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
