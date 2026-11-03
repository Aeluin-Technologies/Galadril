<script setup lang="ts">
import { VueFlow } from "@vue-flow/core";
import { Background } from "@vue-flow/background";
import { stringify } from "yaml";
import type { NodeProps } from "@vue-flow/core";
import type { EditorNodeData } from "~/composables/usePipelineEditor";
import { reviewChanges } from "~/utils/pipeline-review";
import { graphFromDefinition } from "~/utils/pipeline";
import type { Definition } from "~/utils/pipeline";
import "@vue-flow/core/dist/style.css";
import "@vue-flow/core/dist/theme-default.css";
const props = defineProps<{ definition: Definition; before?: Definition }>();
const id = useId();
const beforeLines = computed(() =>
  stringify(props.before ?? props.definition).split("\n"),
);
const afterLines = computed(() => stringify(props.definition).split("\n"));
const mode = ref("graph");
const selected = ref("");
const changes = computed(() =>
  reviewChanges(props.before ?? props.definition, props.definition),
);
const inspected = computed(() =>
  graphFromDefinition(props.definition).nodes.find(
    (node) => node.id === selected.value,
  ),
);
const change = computed(() =>
  changes.value.find(
    (item) =>
      item.id === `step:${selected.value}` ||
      item.id === `source:${selected.value}`,
  ),
);
const graph = computed(() => {
  const value = graphFromDefinition(props.definition);
  return {
    nodes: value.nodes.map((node) => ({
      ...node,
      type: "custom",
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
    <nav
      class="flex gap-2 border-b border-border bg-surface px-4 py-2"
      :aria-label="$t('pipeline.review.views')"
    >
      <UiButton
        v-for="view in ['graph', 'code']"
        :key="view"
        variant="ghost"
        :aria-pressed="mode === view"
        @click="mode = view"
        >{{ $t(`pipeline.review.${view}`) }}</UiButton
      >
    </nav>
    <div v-if="mode === 'graph'" class="h-[28rem]">
      <VueFlow
        :id="id"
        :nodes="graph.nodes"
        :edges="graph.edges"
        :nodes-draggable="false"
        :nodes-connectable="false"
        :elements-selectable="true"
        :delete-key-code="null"
        @node-click="({ node }) => (selected = node.id)"
        :fit-view-params="{ padding: 0.3, maxZoom: 1 }"
        fit-view-on-init
        ><Background :gap="24" :size="0.6" />
        <template #node-custom="node: NodeProps<EditorNodeData>">
          <div
            :class="
              changes.some(
                (item) =>
                  item.id === `step:${node.id}` ||
                  item.id === `source:${node.id}`,
              )
                ? 'ring-2 ring-primary ring-offset-2'
                : ''
            "
          >
            <PipelineEditorNode
              :id="node.id"
              :data="node.data"
              readonly
              :selected="selected === node.id"
              @configure="selected = node.id"
            />
          </div>
        </template>
      </VueFlow>
    </div>
    <aside
      v-if="mode === 'graph' && inspected"
      class="grid gap-3 border-t border-border bg-surface p-4 md:grid-cols-2"
      :aria-label="$t('pipeline.inspector.title')"
    >
      <h2 class="font-semibold md:col-span-2">
        {{ inspected.data.title ?? inspected.id }}
        <UiBadge v-if="change">{{
          $t(`pipeline.review.${change.kind}`)
        }}</UiBadge>
      </h2>
      <div>
        <h3 class="text-sm font-semibold">
          {{ $t("pipeline.review.before") }}
        </h3>
        <pre class="overflow-auto whitespace-pre-wrap text-xs">{{
          stringify(change?.before ?? inspected.data.config)
        }}</pre>
      </div>
      <div>
        <h3 class="text-sm font-semibold">{{ $t("pipeline.review.after") }}</h3>
        <pre class="overflow-auto whitespace-pre-wrap text-xs">{{
          stringify(change?.after ?? inspected.data.config)
        }}</pre>
      </div>
    </aside>
    <div
      v-if="mode === 'code'"
      role="region"
      :aria-label="$t('pipeline.review.yamlComparison')"
      class="grid max-h-[36rem] gap-px overflow-auto bg-border md:grid-cols-2"
    >
      <div class="min-w-0 bg-surface p-4">
        <h3 class="mb-3 font-semibold">{{ $t("pipeline.review.before") }}</h3>
        <pre
          class="overflow-auto text-xs"
        ><code><span v-for="(line, index) in beforeLines" :key="index" class="block px-2" :class="line !== afterLines[index] ? 'bg-danger-subtle' : ''"><span class="mr-3 select-none text-muted">{{ index + 1 }}</span>{{ line }}</span></code></pre>
      </div>
      <div class="min-w-0 bg-surface p-4">
        <h3 class="mb-3 font-semibold">{{ $t("pipeline.review.after") }}</h3>
        <pre
          class="overflow-auto text-xs"
        ><code><span v-for="(line, index) in afterLines" :key="index" class="block px-2" :class="line !== beforeLines[index] ? 'bg-success-subtle' : ''"><span class="mr-3 select-none text-muted">{{ index + 1 }}</span>{{ line }}</span></code></pre>
      </div>
    </div>
  </section>
</template>
