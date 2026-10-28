<script setup lang="ts">
import { XMarkIcon } from "@heroicons/vue/24/outline";
import { branchLabels, copyConfig } from "~/utils/pipeline";
import type { NodeConfig } from "~/utils/pipeline";
import type { EditorNode } from "~/composables/usePipelineEditor";
const props = defineProps<{
  node: EditorNode;
  nodes: EditorNode[];
  incoming: string;
  branch: string;
  failed?: boolean;
  issues?: readonly import("~/utils/pipeline").PipelineDiagnostic[];
}>();
const open = defineModel<boolean>({ required: true });
const emit = defineEmits<{
  save: [config: NodeConfig, upstream: string, branch: string];
}>();
const draft = ref<NodeConfig>(copyConfig(props.node.data.config));
const upstream = ref(props.incoming),
  label = ref(props.branch);
const advanced = ref(false);
const available = ref(!props.node.data.config.capability_id);
const parent = computed(() =>
  props.nodes.find((node) => node.id === upstream.value),
);
const choices = computed(() =>
  props.nodes
    .filter(
      (node) => node.id !== props.node.id && node.data.stepType !== "pipeline",
    )
    .map((node) => ({ value: node.id, label: node.data.title ?? node.id })),
);
const strategy = computed({
  get: () => (draft.value.routing === "conditions" ? "conditions" : "advanced"),
  set: (value: string) => {
    if (value === "conditions")
      draft.value = {
        ...draft.value,
        routing: "conditions",
        params: {
          conditions: [],
          match_branch: "match",
          default_branch: "other",
        },
        branch_labels: ["match", "other"],
      };
    else
      draft.value = {
        ...draft.value,
        routing: "regex",
        params: {
          field: "record.raw_payload.content",
          pattern: "",
          match_branch: "match",
          default_branch: "other",
        },
      };
  },
});
watch(upstream, () => {
  label.value =
    parent.value?.data.stepType === "route"
      ? (branchLabels(parent.value.data.config)[0] ?? "")
      : "";
});
</script>
<template>
  <aside
    v-if="open"
    role="region"
    :aria-label="$t('pipeline.composition.configure')"
    class="min-w-0 overflow-y-auto border-t border-border bg-surface p-4 lg:border-s lg:border-t-0"
  >
    <div class="mb-4 flex items-center justify-between gap-2">
      <div class="min-w-0">
        <h2 class="font-medium">{{ $t("pipeline.inspector.title") }}</h2>
        <p class="break-all text-xs text-muted">
          {{ node.data.title ?? node.id }}
        </p>
      </div>
      <UiIconButton
        :icon="XMarkIcon"
        :label="$t('ui.actions.close')"
        variant="ghost"
        @click="open = false"
      />
    </div>
    <form
      class="space-y-4"
      @submit.prevent="emit('save', draft, upstream, label)"
    >
      <UiAlert v-for="(issue, index) in issues ?? []" :key="index" tone="danger"
        ><p>{{ $t(`pipeline.diagnostics.codes.${issue.code}`) }}</p>
        <p class="mt-1 text-xs">
          {{ $t(`pipeline.diagnostics.help.${issue.code}`) }}
        </p></UiAlert
      >
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
        @available="available = $event"
      />
      <template v-else-if="node.data.stepType === 'route'">
        <UiSelect
          v-model="strategy"
          :label="$t('pipeline.conditions.method')"
          :options="[
            { value: 'conditions', label: $t('pipeline.conditions.business') },
            { value: 'advanced', label: $t('pipeline.conditions.advanced') },
          ]"
        />
        <PipelineConditionsForm
          v-if="strategy === 'conditions'"
          v-model="draft"
          mode="route"
        />
        <PipelineRoutingForm v-else v-model="draft" />
      </template>
      <PipelineConditionsForm
        v-else-if="node.data.stepType === 'filter'"
        v-model="draft"
        mode="filter"
      />
      <p
        v-else-if="node.data.stepType === 'forward'"
        class="text-sm text-muted"
      >
        {{ $t("pipeline.inspector.unionHelp") }}
      </p>
      <p
        v-else-if="node.data.stepType === 'pipeline'"
        class="break-all text-sm text-muted"
      >
        {{ draft.pipeline_ref?.pipeline_id }} ·
        {{ draft.pipeline_ref?.revision_id }}
      </p>
      <PipelineNativeSettings
        v-else
        v-model="draft"
        :kind="node.data.stepType"
      />
      <details>
        <summary class="cursor-pointer text-sm text-muted">
          {{ $t("pipeline.plugins.advanced") }}
        </summary>
        <div class="mt-3 space-y-3">
          <UiCheckbox
            v-model="advanced"
            :label="$t('pipeline.inspector.showConfig')"
          />
          <pre
            v-if="advanced"
            class="max-h-64 overflow-auto whitespace-pre-wrap break-all text-xs"
            >{{ JSON.stringify(draft, null, 2) }}</pre>
        </div>
      </details>
      <UiButton :disabled="!available" type="submit" class="w-full">{{
        $t("pipeline.composition.apply")
      }}</UiButton>
    </form>
  </aside>
</template>
