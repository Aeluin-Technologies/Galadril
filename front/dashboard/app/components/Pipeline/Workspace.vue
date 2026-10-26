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
const { locale, t } = useI18n();
const props = defineProps<{ pipelineId?: string }>();
const { session } = useAccount();
let active = true;
let epoch = 0;
const labels = shallowRef<PluginBlock[]>([]);
const catalog = usePipelineCatalog();
const artifact = shallowRef<PipelineArtifact>();
const loading = ref(!!props.pipelineId);
const busy = ref(false);
const notice = ref("");
const operationError = ref("");
const message = ref("");
const deleting = ref(false);
const historyOpen = ref(false);
const publishedDefinition = ref("");
const publishedCurrent = computed(
  () =>
    artifact.value?.published_revision_id != null &&
    publishedDefinition.value === JSON.stringify(artifact.value?.definition),
);
const identifier = ref(
  props.pipelineId ?? `pipeline-${crypto.randomUUID().slice(0, 8)}`,
);
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
const savedGraph = ref("");
function signature() {
  return JSON.stringify({
    name: name.value,
    nodes: nodes.value.map((node) => node.data),
    edges: edges.value.map((edge) => [
      edge.source,
      edge.target,
      edge.sourceHandle,
    ]),
  });
}
const dirty = computed(() => savedGraph.value !== signature());
function checkpoint(value: PipelineArtifact, snapshot = signature()) {
  artifact.value = value;
  savedGraph.value = snapshot;
}
async function refresh() {
  if (!props.pipelineId) return;
  const request = ++epoch;
  loading.value = true;
  operationError.value = "";
  try {
    const result = await catalog.read(props.pipelineId);
    const available = await catalog.capabilities().catch(() => []);
    if (request !== epoch) return;
    labels.value = available ?? [];
    editor.load(result, labels.value);
    checkpoint(result);
    if (result.published_revision_id) {
      const published =
        result.published_revision_id === result.head_revision_id
          ? result
          : await catalog.read(props.pipelineId, result.published_revision_id);
      if (request !== epoch) return;
      publishedDefinition.value = JSON.stringify(published.definition);
    }
  } catch {
    if (request === epoch)
      operationError.value = t("pipeline.library.loadError");
  } finally {
    if (request === epoch) loading.value = false;
  }
}
async function persist() {
  if (busy.value || (props.pipelineId && !artifact.value)) return;
  busy.value = true;
  notice.value = "";
  operationError.value = "";
  try {
    const snapshot = signature();
    const definition = await editor.validate();
    const result = await catalog.save(
      identifier.value,
      definition,
      artifact.value?.head_revision_id,
      message.value,
    );
    checkpoint(result, snapshot);
    message.value = "";
    notice.value = t("pipeline.library.saved");
    if (!props.pipelineId)
      await navigateTo(`/pipeline/${encodeURIComponent(identifier.value)}`, {
        replace: true,
      });
  } catch {
    operationError.value = t("pipeline.library.saveError");
  } finally {
    busy.value = false;
  }
}
async function publish() {
  if (!artifact.value || busy.value || dirty.value) return;
  busy.value = true;
  operationError.value = "";
  notice.value = "";
  try {
    checkpoint(
      await catalog.publish(identifier.value, artifact.value.head_revision_id),
      savedGraph.value,
    );
    notice.value = t("pipeline.library.publishedNotice");
    publishedDefinition.value = JSON.stringify(artifact.value?.definition);
  } catch {
    operationError.value = t("pipeline.library.publishError");
  } finally {
    busy.value = false;
  }
}
async function retire() {
  if (!artifact.value || busy.value) return;
  busy.value = true;
  operationError.value = "";
  try {
    await catalog.remove(identifier.value, artifact.value.head_revision_id);
    deleting.value = false;
    await navigateTo("/pipeline");
  } catch {
    operationError.value = t("pipeline.library.deleteError");
  } finally {
    busy.value = false;
  }
}
onMounted(() => {
  void refresh();
});
function clear() {
  epoch++;
  labels.value = [];
  catalog.cancel();
  artifact.value = undefined;
  publishedDefinition.value = "";
  editor.clear();
  notice.value = "";
  operationError.value = "";
  savedGraph.value = "";
  historyOpen.value = false;
  busy.value = false;
  loading.value = false;
}
onDeactivated(() => {
  active = false;
  clear();
});
onActivated(() => {
  active = true;
  if (props.pipelineId && !artifact.value && !loading.value) void refresh();
});
watch([() => session.value?.tenant, () => session.value?.subject], () => {
  clear();
  if (active) void refresh();
});
onMounted(() => window.addEventListener("pengolo-access-invalidated", clear));
onBeforeUnmount(() =>
  window.removeEventListener("pengolo-access-invalidated", clear),
);
async function loadVersion(revision: string) {
  if (busy.value || !artifact.value) return;
  const request = epoch;
  busy.value = true;
  operationError.value = "";
  try {
    const result = await catalog.read(identifier.value, revision);
    if (request !== epoch) return;
    editor.load(result, labels.value);
    notice.value = t("pipeline.library.versionLoaded");
  } catch {
    if (request === epoch)
      operationError.value = t("pipeline.library.loadError");
  } finally {
    if (request === epoch) busy.value = false;
  }
}
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
    <div class="flex flex-wrap items-center justify-between gap-3">
      <NuxtLink to="/pipeline" class="text-sm text-info">{{
        $t("pipeline.library.back")
      }}</NuxtLink>
      <div class="flex flex-wrap items-center gap-2">
        <span v-if="artifact" class="text-sm text-muted">{{
          publishedCurrent
            ? $t("pipeline.library.published")
            : $t("pipeline.library.draft")
        }}</span>
        <UiButton
          :disabled="
            loading || busy || !dirty || (!!props.pipelineId && !artifact)
          "
          :loading="busy"
          @click="persist"
          >{{ $t("pipeline.library.save") }}</UiButton
        >
        <UiButton
          v-if="artifact"
          variant="secondary"
          :disabled="loading || busy || dirty || publishedCurrent"
          @click="publish"
          >{{ $t("pipeline.library.publish") }}</UiButton
        >
        <UiButton
          v-if="artifact"
          variant="ghost"
          :disabled="busy"
          @click="deleting = true"
          >{{ $t("pipeline.library.delete") }}</UiButton
        >
      </div>
    </div>
    <p v-if="loading" role="status">{{ $t("pipeline.library.loading") }}</p>
    <UiAlert v-if="operationError" tone="danger">{{ operationError }}</UiAlert>
    <UiButton
      v-if="operationError && !artifact"
      variant="secondary"
      :disabled="loading"
      @click="refresh"
      >{{ $t("pipeline.library.retry") }}</UiButton
    >
    <p v-if="notice" role="status" class="text-sm text-success">{{ notice }}</p>
    <details v-if="artifact" class="text-sm text-muted">
      <summary @click="historyOpen = true">
        {{ $t("pipeline.library.version") }}
      </summary>
      <div class="mt-3 space-y-3">
        <p class="break-all">{{ artifact.head_revision_id }}</p>
        <UiInput
          v-model="message"
          :aria-label="$t('pipeline.library.message')"
          :placeholder="$t('pipeline.library.message')"
          maxlength="1024"
        />
        <PipelineHistory
          v-if="historyOpen"
          :pipeline-id="identifier"
          :revision="artifact.head_revision_id"
          @select="loadVersion"
        />
      </div>
    </details>
    <UiDialog
      :open="deleting"
      :title="$t('pipeline.library.delete')"
      @close="deleting = false"
      ><p>{{ $t("pipeline.library.deleteHelp") }}</p>
      <template #footer
        ><UiButton variant="secondary" @click="deleting = false">{{
          $t("pipeline.library.cancel")
        }}</UiButton
        ><UiButton variant="danger" :loading="busy" @click="retire">{{
          $t("pipeline.library.delete")
        }}</UiButton></template
      ></UiDialog
    >
    <template v-if="!loading && (!props.pipelineId || artifact)">
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
    </template>
  </section>
</template>
