<script setup lang="ts">
import { branchLabels, copyConfig } from "~/utils/pipeline";
import type { NodeConfig } from "~/utils/pipeline";
import type { EditorNode } from "~/composables/usePipelineEditor";
const props = defineProps<{
  node: EditorNode;
  nodes: EditorNode[];
  incoming: string;
  branch: string;
  failed?: boolean;
}>();
const open = defineModel<boolean>({ required: true });
const emit = defineEmits<{
  save: [config: NodeConfig, upstream: string, branch: string];
}>();
const pluginValid = ref(!props.node.data.config.capability_id);
const draft = ref<NodeConfig>(copyConfig(props.node.data.config));
const upstream = ref(props.incoming),
  label = ref(props.branch);
const draftNode = computed(() => ({
  ...props.node,
  data: { ...props.node.data, config: draft.value },
}));
const parent = computed(() =>
  props.nodes.find((node) => node.id === upstream.value),
);
const choices = computed(() =>
  props.nodes
    .filter(
      (node) => node.id !== props.node.id && node.data.stepType !== "pipeline",
    )
    .map((node) => ({ value: node.id, label: node.id })),
);
watch(upstream, () => {
  label.value =
    parent.value?.data.stepType === "route"
      ? (branchLabels(parent.value.data.config)[0] ?? "")
      : "";
});
</script>
<template>
  <UiDialog v-model:open="open" :title="$t('pipeline.composition.configure')">
    <div class="grid gap-4">
      <UiAlert v-if="failed" tone="danger">{{
        $t("pipeline.composition.invalid")
      }}</UiAlert>
      <UiSelect
        v-if="node.data.stepType !== 'source'"
        v-model="upstream"
        :label="$t('pipeline.composition.incoming')"
        :options="[
          { value: '', label: $t('pipeline.composition.unconnected') },
          ...choices,
        ]"
      />
      <UiSelect
        v-if="parent?.data.stepType === 'route'"
        v-model="label"
        :label="$t('pipeline.composition.branch')"
        :options="
          branchLabels(parent.data.config).map((value) => ({
            value,
            label: value,
          }))
        "
      />
      <PipelinePluginSettings
        v-if="node.data.config.capability_id"
        v-model="draft"
        @valid="pluginValid = $event"
      />
      <PipelineRoutingForm
        v-else-if="node.data.stepType === 'route'"
        v-model="draft"
      />
      <p
        v-else-if="node.data.stepType === 'pipeline'"
        class="break-all text-muted"
      >
        {{ draft.pipeline_ref?.pipeline_id }} ·
        {{ draft.pipeline_ref?.revision_id }}
      </p>
      <PipelineSidebar
        v-else
        :active-node="draftNode"
        :has-incoming="Boolean(upstream)"
        @close="open = false"
      />
      <UiButton
        :disabled="!pluginValid"
        @click="emit('save', draft, upstream, label)"
        >{{ $t("pipeline.composition.apply") }}</UiButton
      >
    </div>
  </UiDialog>
</template>
