<script setup lang="ts">
import { useOnboardingStore } from "~/stores/useOnboarding";
import { pipelinePalette } from "~/utils/pipeline-visuals";
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
import type {
  AuthoringCommand,
  NodeConfig,
  PipelineArtifact,
  StepKind,
} from "~/utils/pipeline";
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
const view = ref<"edit" | "proposals" | "history">("edit");
const checking = ref(false);
let validationTimer: ReturnType<typeof setTimeout> | undefined;
const branch = ref("main");
const conflicted = ref(false);
const configurationVersion = ref(0);
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
const draftDefinition = computed(() => editor.draft());
function signature() {
  return JSON.stringify(editor.draft());
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
    const requested = useRoute().query.branch;
    if (typeof requested === "string") switchBranch(requested);
  } catch {
    if (request === epoch)
      operationError.value = t("pipeline.library.loadError");
  } finally {
    if (request === epoch) loading.value = false;
  }
}
async function persist() {
  if (busy.value || (props.pipelineId && !artifact.value)) return;
  const request = epoch;
  busy.value = true;
  notice.value = "";
  operationError.value = "";
  try {
    const snapshot = signature();
    const definition = editor.draft();
    const result = await catalog.save(
      identifier.value,
      definition,
      artifact.value?.head_revision_id,
      message.value,
      branch.value,
    );
    if (request !== epoch) return;
    checkpoint(result, snapshot);
    useOnboardingStore().setStepCompleted("pipeline");
    message.value = "";
    notice.value = t("pipeline.library.saved");
    if (!props.pipelineId)
      await navigateTo(`/pipeline/${encodeURIComponent(identifier.value)}`, {
        replace: true,
      });
  } catch (error) {
    if (request !== epoch) return;
    conflicted.value = error instanceof Error && error.message === "CONFLICT";
    operationError.value = t(
      conflicted.value
        ? "pipeline.collaboration.diverged"
        : "pipeline.library.saveError",
    );
  } finally {
    if (request === epoch) busy.value = false;
  }
}
async function transition(command: AuthoringCommand) {
  if (!artifact.value || busy.value || dirty.value) return;
  const request = epoch;
  busy.value = true;
  operationError.value = "";
  notice.value = "";
  try {
    let current = artifact.value;
    if (command.operation === "publish" && current.lifecycle !== "approved") {
      current = await catalog.author(
        identifier.value,
        current.definition,
        { operation: "approve" },
        current.head_revision_id,
      );
      if (request !== epoch) return;
      checkpoint(current, savedGraph.value);
    }
    const result = await catalog.author(
      identifier.value,
      current.definition,
      command,
      current.head_revision_id,
    );
    if (request !== epoch) return;
    checkpoint(result, savedGraph.value);
    notice.value = t(`pipeline.lifecycle.done.${command.operation}`);
  } catch (error) {
    if (request === epoch)
      operationError.value = t(
        error instanceof Error && error.message === "CONFLICT"
          ? "pipeline.collaboration.diverged"
          : "pipeline.lifecycle.failed",
      );
  } finally {
    if (request === epoch) busy.value = false;
  }
}
function switchBranch(value: string | undefined) {
  if (!value) return;
  if (!artifact.value || dirty.value || busy.value) return;
  const definition =
    value === "main"
      ? artifact.value.definition
      : artifact.value.authoring?.branches[value]?.definition;
  if (!definition) return;
  branch.value = value;
  editor.load(
    { ...artifact.value, name: definition.name, definition },
    labels.value,
  );
  savedGraph.value = signature();
  configuring.value = false;
}
function changedWorkspace(value: PipelineArtifact) {
  artifact.value = value;
  const definition =
    branch.value === "main"
      ? value.definition
      : value.authoring?.branches[branch.value]?.definition;
  if (!definition) return;
  editor.load({ ...value, name: definition.name, definition }, labels.value);
  savedGraph.value = signature();
  notice.value = t("pipeline.collaboration.updated");
}
function rebased(
  value: PipelineArtifact,
  definition: import("~/utils/pipeline").Definition,
) {
  changedWorkspace(value);
  editor.load({ ...value, definition, name: definition.name }, labels.value);
  conflicted.value = false;
  operationError.value = "";
}
async function retire() {
  if (!artifact.value || busy.value) return;
  const request = epoch;
  busy.value = true;
  operationError.value = "";
  try {
    await catalog.remove(identifier.value, artifact.value.head_revision_id);
    if (request !== epoch) return;
    deleting.value = false;
    await navigateTo("/pipeline");
  } catch {
    if (request === epoch)
      operationError.value = t("pipeline.library.deleteError");
  } finally {
    if (request === epoch) busy.value = false;
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
  branch.value = "main";
  conflicted.value = false;
  clearTimeout(validationTimer);
  editor.clear();
  notice.value = "";
  operationError.value = "";
  savedGraph.value = "";
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
  view.value = "edit";
  const selectedNode = flow.findNode(id);
  if (selectedNode) flow.addSelectedNodes([selectedNode]);
  void nextTick(() => flow.fitView({ nodes: [id], padding: 0.7, maxZoom: 1 }));
  configurationVersion.value++;
  failure.value = false;
  selected.value = id;
  configuring.value = true;
}
function save(config: NodeConfig, upstream: string, branch: string) {
  if (!selected.value) return;
  try {
    editor.configure(selected.value, config, upstream, branch);
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
function arrange() {
  try {
    editor.arrange();
    void nextTick(() => flow.fitView({ padding: 0.2, maxZoom: 1 }));
  } catch {
    failure.value = true;
  }
}
async function validate() {
  if (!active || loading.value || checking.value) return;
  const checkedSignature = signature();
  checking.value = true;
  try {
    await editor.validate();
  } catch {
    /* Diagnostics own validation failures. */
  } finally {
    checking.value = false;
    if (active && checkedSignature !== signature()) {
      clearTimeout(validationTimer);
      validationTimer = setTimeout(() => {
        void validate();
      }, 1200);
    }
  }
}
watch(
  signature,
  () => {
    clearTimeout(validationTimer);
    validationTimer = setTimeout(() => {
      void validate();
    }, 1200);
  },
  { immediate: true },
);
onBeforeUnmount(() => clearTimeout(validationTimer));
</script>
<template>
  <section
    class="flex min-h-[560px] min-w-0 flex-col bg-canvas"
    :aria-label="$t('pipeline.composition.editor')"
    @keydown="keyboard"
  >
    <PipelineCommandBar
      v-model="name"
      v-model:view="view"
      :artifact="artifact"
      :branch="branch"
      :dirty="dirty"
      :busy="busy"
      :loading="loading"
      :can-approve="validated && !checking && !editor.diagnostics.value.length"
      @save="persist"
      @branch="switchBranch"
      @transition="(operation) => transition({ operation })"
      @remove="deleting = true"
      @failure="failure = true"
    />
    <p v-if="loading" role="status">{{ $t("pipeline.library.loading") }}</p>
    <UiAlert v-if="operationError" tone="danger">{{ operationError }}</UiAlert>
    <UiButton
      v-if="operationError && !artifact"
      variant="secondary"
      :disabled="loading"
      @click="refresh"
      >{{ $t("pipeline.library.retry") }}</UiButton
    >
    <PipelineRebase
      v-if="conflicted && artifact"
      :artifact="artifact"
      :definition="draftDefinition"
      :branch="branch"
      @apply="rebased"
    />
    <section
      v-if="view !== 'edit'"
      :id="`pipeline-panel-${view}`"
      role="tabpanel"
      :aria-labelledby="`pipeline-tab-${view}`"
      class="min-w-0 p-4 sm:p-6"
    >
      <PipelineCollaboration
        v-if="artifact"
        :artifact="artifact"
        :branch="branch"
        :disabled="busy || dirty"
        :mode="view"
        @changed="changedWorkspace"
        @checkout="switchBranch"
      /><UiEmptyState
        v-else
        :title="$t('pipeline.chrome.saveForHistory')"
        :description="$t('pipeline.chrome.saveForHistoryHelp')"
      />
    </section>
    <p v-if="notice" role="status" class="text-sm text-success">{{ notice }}</p>
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
    <div
      v-show="view === 'edit'"
      id="pipeline-panel-edit"
      role="tabpanel"
      aria-labelledby="pipeline-tab-edit"
      class="min-w-0"
      v-if="!loading && (!props.pipelineId || artifact)"
    >
      <PipelineToolbar
        :blocks="nodes.length"
        :transfers="
          nodes.filter((node) => node.data.stepType === 'pipeline').length
        "
        :can-undo="canUndo"
        :can-redo="canRedo"
        :checking="checking"
        :validated="validated"
        @add="add"
        @undo="editor.undo"
        @redo="editor.redo"
        @arrange="arrange"
        @failure="failure = true"
      />
      <UiAlert v-if="failure" tone="danger">{{
        $t("pipeline.composition.invalid")
      }}</UiAlert>
      <UiAlert v-if="validated" tone="success">{{
        $t("pipeline.composition.validated", { count: expandedBlocks })
      }}</UiAlert>
      <PipelineDiagnostics
        :issues="editor.diagnostics.value"
        :nodes="nodes"
        :edges="edges"
        @select="configure"
        @repair="
          (repaired) => {
            edges = repaired;
            editor.remember();
          }
        "
      />
      <div
        class="grid min-w-0 overflow-hidden border-b border-border"
        :class="
          configuring && current ? 'lg:grid-cols-[minmax(0,1fr)_340px]' : ''
        "
      >
        <div
          ref="canvas"
          class="relative h-[calc(100dvh-250px)] min-h-[440px] min-w-0 overflow-hidden bg-canvas"
          role="region"
          :aria-label="$t('pipeline.composition.canvas')"
        >
          <VueFlow
            id="pipeline-editor"
            v-model:nodes="nodes"
            v-model:edges="edges"
            :fit-view-on-init="true"
            :default-edge-options="{
              type: 'smoothstep',
              style: {
                stroke: 'rgb(var(--ui-border-control))',
                strokeWidth: 1.5,
              },
            }"
            :fit-view-params="{ padding: 0.2, maxZoom: 1 }"
            :min-zoom="0.1"
            @nodes-initialized="flow.fitView({ padding: 0.2, maxZoom: 1 })"
            :delete-key-code="null"
            @connect="editor.connect"
            @node-click="({ node }) => configure(node.id)"
            @node-drag-stop="editor.remember"
            @edges-change="validated = false"
          >
            <Background :gap="24" :size="0.6" />
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
                :selected="node.selected"
                :data="node.data"
                :issues="
                  editor.diagnostics.value.filter(
                    (issue) => issue.nodeId === node.id,
                  )
                "
                @configure="configure(node.id)"
                @remove="editor.remove(node.id)"
              />
            </template>
          </VueFlow>
          <details
            class="absolute start-3 top-3 max-w-[calc(100%-24px)] border border-border bg-surface/95 p-2 text-xs text-muted"
          >
            <summary class="cursor-pointer">
              {{ $t("pipeline.chrome.legend") }}
            </summary>
            <ul class="mt-2 flex flex-wrap gap-3">
              <li
                v-for="(item, key) in pipelinePalette"
                :key="key"
                class="flex items-center gap-1"
              >
                <span
                  class="h-2.5 w-2.5"
                  :style="{ backgroundColor: item.color }"
                  aria-hidden="true"
                />{{ $t(item.label) }}
              </li>
            </ul>
          </details>
        </div>
        <PipelineBlockSettings
          v-if="configuring && current"
          :key="`${current.id}:${configurationVersion}`"
          :issues="
            editor.diagnostics.value.filter(
              (issue) => issue.nodeId === current?.id,
            )
          "
          v-model="configuring"
          :node="current"
          :failed="failure"
          :nodes="nodes"
          :incoming="incoming?.source ?? ''"
          :branch="incoming?.sourceHandle ?? ''"
          @save="save"
        />
      </div>
      <PipelinePluginPicker v-model="pluginsOpen" @select="pluginSelected" />
      <PipelineLinkPicker v-model="linking" @select="linked" />
    </div>
  </section>
</template>
