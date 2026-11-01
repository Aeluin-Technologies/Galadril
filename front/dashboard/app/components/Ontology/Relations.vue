<script setup lang="ts">
import { VueFlow } from "@vue-flow/core";
import type { Node, Edge } from "@vue-flow/core";
import "@vue-flow/core/dist/style.css";
import "@vue-flow/core/dist/theme-default.css";
defineProps<{ nodes: Node[]; edges: Edge[] }>();
const emit = defineEmits<{ select: [id: string] }>();
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
      class="h-[320px]"
      :aria-label="$t('ontologyManager.ontology_relationship_graph')"
    >
      <VueFlow
        :nodes="nodes"
        :edges="edges"
        :nodes-draggable="false"
        :nodes-connectable="false"
        fit-view-on-init
        @node-click="({ node }) => emit('select', node.id)"
      />
    </div>
  </section>
</template>
