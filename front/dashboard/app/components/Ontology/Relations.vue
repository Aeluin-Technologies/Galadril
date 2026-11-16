<script setup lang="ts">
import { VueFlow } from "@vue-flow/core";
import { Background } from "@vue-flow/background";
import { CubeIcon } from "@heroicons/vue/24/outline";
import type { Node, Edge } from "@vue-flow/core";
import "@vue-flow/core/dist/style.css";
import "@vue-flow/core/dist/theme-default.css";
defineProps<{ nodes: Node[]; edges: Edge[] }>();
const emit = defineEmits<{ select: [key: string] }>();
const id = useId();
</script>
<template>
  <section
    class="rounded-panel border border-border bg-surface"
    :aria-label="$t('gol.relationships')"
  >
    <h2 class="border-b border-border px-4 py-3 font-semibold">
      {{ $t("gol.relationships") }}
    </h2>
    <div
      class="h-[min(52vh,540px)] min-h-[320px] bg-canvas"
      :aria-label="$t('gol.graph')"
    >
      <VueFlow
        :id="id"
        :nodes="nodes.map((node) => ({ ...node, type: 'ontology' }))"
        :edges="edges"
        :nodes-draggable="false"
        :nodes-connectable="false"
        :delete-key-code="null"
        :fit-view-params="{ padding: 0.2, maxZoom: 1 }"
        fit-view-on-init
        @node-click="({ node }) => emit('select', node.id)"
      >
        <Background :gap="24" :size="0.6" />
        <template #node-ontology="{ id: key, data, selected }"
          ><div
            class="w-60 rounded-control border bg-surface shadow-panel"
            :class="
              selected
                ? 'border-focus ring-2 ring-focus'
                : 'border-border-control'
            "
          >
            <UiButton
              variant="ghost"
              align="start"
              class="w-full"
              @click.stop="emit('select', key)"
              ><CubeIcon class="mr-2 h-4 w-4" aria-hidden="true" />{{
                data.label
              }}</UiButton
            >
            <p class="break-all px-3 pb-3 font-mono text-xs text-muted">
              {{ key }}
            </p>
          </div></template
        >
      </VueFlow>
    </div>
  </section>
</template>
