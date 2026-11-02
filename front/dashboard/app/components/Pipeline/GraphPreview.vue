<script setup lang="ts">
import { VueFlow } from "@vue-flow/core";
import { Background } from "@vue-flow/background";
import { graphFromDefinition } from "~/utils/pipeline";
import type { Definition } from "~/utils/pipeline";
import "@vue-flow/core/dist/style.css";
import "@vue-flow/core/dist/theme-default.css";
const props = defineProps<{ definition: Definition }>();
const id = useId();
const graph = computed(() => {
  const value = graphFromDefinition(props.definition);
  return {
    nodes: value.nodes.map((node) => ({
      ...node,
      type: "default",
      data: { label: node.data.title ?? node.id },
      style: {
        background: "rgb(var(--ui-surface))",
        borderColor: "rgb(var(--ui-primary))",
        color: "rgb(var(--ui-foreground))",
        fontSize: "12px",
      },
    })),
    edges: value.edges.map((edge) => ({
      ...edge,
      type: "smoothstep",
      style: { stroke: "rgb(var(--ui-border-control))" },
    })),
  };
});
</script>
<template>
  <section
    class="rounded-panel border border-border bg-canvas"
    :aria-label="$t('pipeline.review.graphPreview')"
  >
    <h2 class="border-b border-border bg-surface px-4 py-2 text-sm font-medium">
      {{ $t("pipeline.review.graphPreview") }}
    </h2>
    <div class="h-72">
      <VueFlow
        :id="id"
        :nodes="graph.nodes"
        :edges="graph.edges"
        :nodes-draggable="false"
        :nodes-connectable="false"
        :elements-selectable="false"
        :fit-view-params="{ padding: 0.3, maxZoom: 1 }"
        fit-view-on-init
        ><Background :gap="24" :size="0.6"
      /></VueFlow>
    </div>
  </section>
</template>
