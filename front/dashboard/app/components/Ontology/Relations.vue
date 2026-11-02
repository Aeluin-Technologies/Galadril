<script setup lang="ts">
import { VueFlow, Handle, Position } from "@vue-flow/core";
import { Background } from "@vue-flow/background";
import {
  CubeIcon,
  EnvelopeIcon,
  UserGroupIcon,
  PaperClipIcon,
} from "@heroicons/vue/24/outline";
import { pipelinePalette } from "~/utils/pipeline-visuals";
import type { Node, Edge, EdgeProps } from "@vue-flow/core";
import "@vue-flow/core/dist/style.css";
import "@vue-flow/core/dist/theme-default.css";
const props = defineProps<{
  nodes: Node[];
  edges: Edge[];
  editable?: boolean;
}>();
const emit = defineEmits<{
  select: [id: string];
  connect: [source: string, target: string];
  inspect: [id: string];
}>();
const id = useId();
const renderedEdges = computed(() => {
  const groups = new Map<string, Edge[]>();
  for (const edge of props.edges) {
    const key = JSON.stringify([edge.source, edge.target]);
    const group = groups.get(key) ?? [];
    group.push(edge);
    groups.set(key, group);
  }
  return [...groups.values()].flatMap((group) =>
    group.map((edge, index) => ({
      ...edge,
      type: "relationship",
      data: { offset: (index - (group.length - 1) / 2) * 36 },
      style: { stroke: "rgb(var(--ui-border-control))", strokeWidth: 1.5 },
    })),
  );
});
function appearance(id: string) {
  if (id.includes("person") || id.includes("address"))
    return { color: pipelinePalette.input.color, icon: UserGroupIcon };
  if (id.includes("message"))
    return { color: pipelinePalette.output.color, icon: EnvelopeIcon };
  if (
    id.includes("attachment") ||
    id.includes("document") ||
    id.includes("content")
  )
    return { color: pipelinePalette.processing.color, icon: PaperClipIcon };
  return { color: pipelinePalette.control.color, icon: CubeIcon };
}
</script>
<template>
  <section
    class="rounded-control border border-border bg-surface"
    :aria-label="$t('ontologyManager.related_types')"
  >
    <h2 class="border-b border-border px-4 py-3 text-sm font-semibold">
      {{ $t("ontologyManager.related_types") }}
    </h2>
    <div
      class="h-[min(62vh,640px)] min-h-[360px] bg-canvas"
      :aria-label="$t('ontologyManager.ontology_relationship_graph')"
    >
      <VueFlow
        :id="id"
        :nodes="nodes.map((node) => ({ ...node, type: 'ontology' }))"
        :edges="renderedEdges"
        :edges-focusable="false"
        :nodes-draggable="editable ?? false"
        :nodes-connectable="editable ?? false"
        :delete-key-code="null"
        :fit-view-params="{ padding: 0.2, maxZoom: 1 }"
        fit-view-on-init
        @node-click="({ node }) => emit('select', node.id)"
        @connect="({ source, target }) => emit('connect', source, target)"
        @edge-click="({ edge }) => emit('inspect', edge.id)"
      >
        <Background :gap="24" :size="0.6" />
        <template #edge-relationship="edge: EdgeProps<{ offset: number }>">
          <OntologyRelationshipEdge
            v-bind="edge"
            :editable="editable"
            @inspect="emit('inspect', edge.id)"
          />
        </template>
        <template #node-ontology="{ id: nodeId, data, selected }">
          <div
            class="relative w-60 border bg-surface text-foreground shadow-sm"
            :class="
              selected
                ? 'border-focus ring-2 ring-focus'
                : 'border-border-control'
            "
          >
            <Handle type="target" :position="Position.Left" />
            <div
              class="flex items-center gap-2 px-3 py-2 text-white"
              :style="{ backgroundColor: appearance(nodeId).color }"
            >
              <component
                :is="appearance(nodeId).icon"
                class="h-4 w-4 shrink-0"
                aria-hidden="true"
              /><strong class="min-w-0 flex-1 break-words text-sm">{{
                data.label
              }}</strong>
            </div>
            <div class="space-y-1 px-3 py-2 text-xs text-muted">
              <p class="truncate font-mono">{{ nodeId }}</p>
              <p>
                {{
                  $t("ontologyManager.propertyCount", {
                    count: data.count ?? 0,
                  })
                }}
              </p>
            </div>
            <Handle type="source" :position="Position.Right" />
          </div>
        </template>
      </VueFlow>
    </div>
  </section>
</template>
