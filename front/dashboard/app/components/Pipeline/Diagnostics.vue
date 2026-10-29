<script setup lang="ts">
import type { Edge } from "@vue-flow/core";
import type { GraphNode, PipelineDiagnostic } from "~/utils/pipeline";
import { proposeConnectionRepair } from "~/utils/pipeline";
const props = defineProps<{
  issues: readonly PipelineDiagnostic[];
  nodes: readonly GraphNode[];
  edges: readonly Edge[];
}>();
const emit = defineEmits<{ select: [id: string]; repair: [edges: Edge[]] }>();
const preview = ref(false);
const proposal = computed(() =>
  proposeConnectionRepair(props.nodes, props.edges),
);
</script>
<template>
  <section
    v-if="issues.length"
    :aria-label="$t('pipeline.diagnostics.title')"
    class="border-b border-border bg-surface px-4 py-2 sm:px-6"
  >
    <div class="flex flex-wrap items-center justify-between gap-2">
      <h2 class="text-sm font-medium">
        {{ $t("pipeline.diagnostics.count", { count: issues.length }) }}
      </h2>
      <UiButton
        v-if="proposal.removed.length"
        variant="secondary"
        size="sm"
        @click="preview = true"
        >{{ $t("pipeline.diagnostics.preview") }}</UiButton
      >
    </div>
    <ul class="mt-2 max-h-32 space-y-1 overflow-y-auto text-sm">
      <li v-for="(issue, index) in issues" :key="index">
        <UiButton
          v-if="issue.nodeId"
          variant="ghost"
          align="start"
          size="sm"
          @click="emit('select', issue.nodeId)"
          >{{
            nodes.find((node) => node.id === issue.nodeId)?.data.title ??
            issue.nodeId
          }}
          · {{ $t(`pipeline.diagnostics.codes.${issue.code}`)
          }}<span v-if="issue.field"> ({{ issue.field }})</span></UiButton
        >
        <p v-else class="text-danger">
          {{ $t(`pipeline.diagnostics.codes.${issue.code}`) }}
        </p>
      </li>
    </ul>
    <UiDialog
      v-model:open="preview"
      :title="$t('pipeline.diagnostics.preview')"
      :description="$t('pipeline.diagnostics.reviewHelp')"
      ><ul class="space-y-2 text-sm">
        <li v-for="edge in proposal.removed" :key="edge.id">
          {{ edge.source }} → {{ edge.target }}
          <span v-if="edge.sourceHandle">· {{ edge.sourceHandle }}</span>
        </li>
      </ul>
      <template #footer
        ><UiButton variant="secondary" @click="preview = false">{{
          $t("pipeline.library.cancel")
        }}</UiButton
        ><UiButton
          @click="
            emit('repair', proposal.edges);
            preview = false;
          "
          >{{ $t("pipeline.diagnostics.apply") }}</UiButton
        ></template
      ></UiDialog
    >
  </section>
</template>
