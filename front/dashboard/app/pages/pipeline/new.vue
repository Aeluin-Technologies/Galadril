<script setup lang="ts">
import {
  VueFlow,
  useVueFlow,
  Handle,
  Position,
  type NodeProps,
} from "@vue-flow/core";
import { Background } from "@vue-flow/background";
import { Controls } from "@vue-flow/controls";

import "@vue-flow/core/dist/style.css";
import "@vue-flow/core/dist/theme-default.css";
import "@vue-flow/controls/dist/style.css";

import {
  TrashIcon,
  PencilSquareIcon,
  CircleStackIcon,
  CpuChipIcon,
  AdjustmentsHorizontalIcon,
  ArrowDownOnSquareIcon,
  BoltIcon,
} from "@heroicons/vue/24/outline";

import { usePipeline } from "@/composables/usePipeline";

const {
  pipelineName,
  pipelineNodes,
  pipelineEdges,
  saveToHistory,
  undo,
  redo,
  exportPipelineYaml,
} = usePipeline();

const {
  toObject,
  onConnect,
  onNodeDragStart,
  getNodes,
  getEdges,
  addSelectedNodes,
  addSelectedEdges,
} = useVueFlow();

const isConfigModalOpen = ref(false);
const activeNodeId = ref<string | null>(null);

const activeNode = computed(() => {
  if (!activeNodeId.value) return null;
  return pipelineNodes.value.find((n) => n.id === activeNodeId.value) || null;
});

const hasIncomingEdges = computed(() => {
  return (nodeId: string) =>
    pipelineEdges.value.some((edge) => edge.target === nodeId);
});

const openConfig = (id: string) => {
  activeNodeId.value = id;
  isConfigModalOpen.value = true;
};

const deleteNode = (id: string) => {
  pipelineNodes.value = pipelineNodes.value.filter((n) => n.id !== id);
  pipelineEdges.value = pipelineEdges.value.filter(
    (e) => e.source !== id && e.target !== id,
  );
  if (activeNodeId.value === id) isConfigModalOpen.value = false;
};

const handleKeyDown = (e: KeyboardEvent) => {
  if (
    ["INPUT", "SELECT", "TEXTAREA"].includes((e.target as HTMLElement).tagName)
  )
    return;

  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === "z") {
    e.preventDefault();
    undo();
  }
  if (
    (e.ctrlKey && e.key.toLowerCase() === "y") ||
    (e.metaKey && e.shiftKey && e.key.toLowerCase() === "z")
  ) {
    e.preventDefault();
    redo();
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") {
    e.preventDefault();
    addSelectedNodes(getNodes.value);
    addSelectedEdges(getEdges.value);
  }
  if (e.key === "Delete" || e.key === "Backspace") {
    const selectedNodes = getNodes.value.filter((n) => n.selected);
    if (selectedNodes.length > 0) {
      e.preventDefault();
      saveToHistory();
      selectedNodes.forEach((node) => deleteNode(node.id));
    }
  }
};

onConnect((params) => {
  saveToHistory();
  pipelineEdges.value.push(params);
});
onNodeDragStart(() => {
  saveToHistory();
});

onMounted(() => window.addEventListener("keydown", handleKeyDown));
onUnmounted(() => window.removeEventListener("keydown", handleKeyDown));

interface NodeMeta {
  icon: any;
  color: string;
  badge: string;
}
const getNodeMeta = (type: string | undefined): NodeMeta => {
  const fallback = {
    icon: markRaw(CpuChipIcon),
    color: "border-purple-200 hover:border-purple-400",
    badge: "bg-purple-50 text-purple-700 border-purple-200",
  };
  if (!type) return fallback;
  const meta: Record<string, NodeMeta> = {
    source: {
      icon: markRaw(CircleStackIcon),
      color: "border-blue-200 hover:border-blue-400",
      badge: "bg-blue-50 text-blue-700 border-blue-200",
    },
    inference: fallback,
    resolve: {
      icon: markRaw(AdjustmentsHorizontalIcon),
      color: "border-amber-200 hover:border-amber-400",
      badge: "bg-amber-50 text-amber-700 border-amber-200",
    },
    sink: {
      icon: markRaw(ArrowDownOnSquareIcon),
      color: "border-emerald-200 hover:border-emerald-400",
      badge: "bg-emerald-50 text-emerald-700 border-emerald-200",
    },
    causal: {
      icon: markRaw(BoltIcon),
      color: "border-rose-200 hover:border-rose-400",
      badge: "bg-rose-50 text-rose-700 border-rose-200",
    },
  };
  return meta[type] || fallback;
};

const addPipelineStep = (
  type: "source" | "inference" | "resolve" | "sink" | "causal",
) => {
  saveToHistory();
  const id = `${type}_${Date.now().toString().slice(-4)}`;
  let defaultConfig: any = { params: {} };

  if (type === "source")
    defaultConfig = {
      topic: "raw-images",
      match_pattern: "^images(/|%2F)",
      schema_path: "schemas/avro/image.avsc",
    };
  else if (type === "inference")
    defaultConfig = {
      model: "galadril_inference.models.face_recognition.FaceRecognitionModel",
      params: { action: "embed" },
    };
  else if (type === "resolve")
    defaultConfig = {
      params: { modality: "face", threshold: 0.85, entity_type: "PERSON" },
    };
  else if (type === "sink")
    defaultConfig = {
      connector: "postgres",
      params: { entity_type: "PERSON", modality: "face" },
    };
  else if (type === "causal")
    defaultConfig = {
      params: { trigger: "cron", cron: "0 * * * *", lookback: "14d" },
    };

  pipelineNodes.value.push({
    id,
    type: "custom",
    position: { x: 300, y: 100 + pipelineNodes.value.length * 50 },
    data: { stepType: type, config: defaultConfig },
  });
};

const handleExport = () => exportPipelineYaml(toObject());
</script>

<template>
  <div
    class="h-full overflow-y-auto p-6 bg-stone-50 font-sans space-y-6 overflow-x-hidden relative"
  >
    <PipelineHeader
      v-model="pipelineName"
      @export="handleExport"
      @add-step="addPipelineStep"
    />

    <div
      class="flex h-[650px] bg-slate-200/50 rounded-xl border border-slate-200 overflow-hidden relative w-full"
    >
      <div class="flex-1 h-full p-4 relative">
        <VueFlow
          v-model:nodes="pipelineNodes"
          v-model:edges="pipelineEdges"
          :fit-view-on-init="true"
          class="w-full h-full"
          :default-edge-options="{
            type: 'smoothstep',
            style: { stroke: '#94a3b8', strokeWidth: 2 },
          }"
        >
          <Background color="#cbd5e1" :gap="20" :size="1.5" />
          <Controls
            class="!bg-white !border !border-slate-200 !rounded-lg !shadow-sm [&_button]:!bg-white [&_button]:!border-slate-100 [&_svg]:!fill-slate-500"
          />

          <template #node-custom="{ id, data }: NodeProps">
            <div
              :class="[
                'w-64 rounded-xl border bg-white p-4 shadow-sm hover:shadow-md transition-shadow group relative cursor-move',
                getNodeMeta(data?.stepType).color,
              ]"
            >
              <Handle
                v-if="data?.stepType !== 'source'"
                type="target"
                :position="Position.Left"
                class="!w-2.5 !h-2.5 !bg-slate-400 !border-2 !border-white transition-all hover:!scale-125"
              />

              <div
                class="absolute top-2 right-2 z-[40] flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity"
              >
                <button
                  @click="openConfig(id)"
                  class="p-1.5 bg-white border border-zinc-200 rounded-md text-zinc-500 hover:text-amber-600 hover:border-amber-300 shadow-sm cursor-pointer"
                >
                  <PencilSquareIcon class="w-3.5 h-3.5 pointer-events-none" />
                </button>
                <button
                  @click="deleteNode(id)"
                  class="p-1.5 bg-white border border-zinc-200 rounded-md text-zinc-500 hover:text-red-600 hover:border-red-300 shadow-sm cursor-pointer"
                >
                  <TrashIcon class="w-3.5 h-3.5 pointer-events-none" />
                </button>
              </div>

              <div class="flex flex-col gap-2">
                <div>
                  <span
                    :class="[
                      'text-[9px] font-mono tracking-widest uppercase font-bold px-2 py-0.5 rounded border',
                      getNodeMeta(data?.stepType).badge,
                    ]"
                  >
                    {{ data?.stepType }}
                  </span>
                </div>

                <div class="flex items-center gap-3 mt-1">
                  <div
                    class="p-2 bg-slate-50 border border-slate-100 rounded-lg text-slate-600"
                  >
                    <component
                      :is="getNodeMeta(data?.stepType).icon"
                      class="w-4 h-4 stroke-[2]"
                    />
                  </div>
                  <div class="flex flex-col min-w-0 flex-1">
                    <span
                      class="font-sans text-sm font-semibold text-zinc-900 truncate"
                      >{{ id }}</span
                    >
                    <span
                      class="text-[11px] text-zinc-500 font-mono truncate mt-0.5"
                      v-if="data?.config?.topic"
                      >{{ data?.config?.topic }}</span
                    >
                    <span
                      class="text-[11px] text-zinc-500 font-mono truncate mt-0.5"
                      v-else-if="data?.config?.model"
                      >{{ data?.config?.model?.split(".").pop() }}</span
                    >
                  </div>
                </div>
              </div>

              <Handle
                v-if="data?.stepType !== 'sink'"
                type="source"
                :position="Position.Right"
                class="!w-2.5 !h-2.5 !bg-slate-400 !border-2 !border-white transition-all hover:!scale-125"
              />
            </div>
          </template>
        </VueFlow>
      </div>

      <PipelineSidebar
        v-if="isConfigModalOpen && activeNode?.data"
        :active-node="activeNode"
        :has-incoming="hasIncomingEdges(activeNode.id)"
        @close="isConfigModalOpen = false"
      />
    </div>
  </div>
</template>
