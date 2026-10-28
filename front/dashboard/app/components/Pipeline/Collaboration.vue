<script setup lang="ts">
import type { AuthoringCommand, PipelineArtifact } from "~/utils/pipeline";
import type {
  DefinitionChange,
  MergePreview,
  ReviewConflict,
} from "~/utils/pipeline-review";
const props = defineProps<{
  artifact: PipelineArtifact;
  branch: string;
  disabled: boolean;
  mode?: "proposals" | "history";
}>();
const emit = defineEmits<{ changed: [artifact: PipelineArtifact] }>();
const catalog = usePipelineCatalog();
const branchName = ref("");
const source = ref(
  props.branch === "main"
    ? (Object.keys(props.artifact.authoring?.branches ?? {})[0] ?? "")
    : props.branch,
);
const busy = ref(false),
  failed = ref(false);
const preview = shallowRef<MergePreview>();
const conflicts = shallowRef<ReviewConflict[]>([]);
const choices = ref<Record<string, "target" | "source">>({});
const changes = shallowRef<DefinitionChange[]>();
const revision = ref("");
let epoch = 0;
async function command(value: AuthoringCommand) {
  if (busy.value || props.disabled) return;
  busy.value = true;
  failed.value = false;
  const own = ++epoch;
  try {
    const result = await catalog.author(
      props.artifact.pipeline_id,
      props.artifact.definition,
      value,
      props.artifact.head_revision_id,
    );
    if (own === epoch) {
      emit("changed", result);
      preview.value = undefined;
      branchName.value = "";
    }
  } catch {
    if (own === epoch) failed.value = true;
  } finally {
    if (own === epoch) busy.value = false;
  }
}
async function review(reset = true) {
  if (!source.value || busy.value) return;
  if (reset) choices.value = {};
  busy.value = true;
  failed.value = false;
  const own = ++epoch;
  try {
    const result = await catalog.mergePreview(
      props.artifact.pipeline_id,
      source.value,
      choices.value,
    );
    if (own === epoch) {
      preview.value = result;
      if (reset) conflicts.value = result.conflicts;
    }
  } catch {
    if (own === epoch) failed.value = true;
  } finally {
    if (own === epoch) busy.value = false;
  }
}
async function compare(value: string) {
  busy.value = true;
  failed.value = false;
  const own = ++epoch;
  try {
    const result = await catalog.comparison(
      props.artifact.pipeline_id,
      value,
      props.branch,
    );
    if (own === epoch) {
      changes.value = result;
      revision.value = value;
    }
  } catch {
    if (own === epoch) failed.value = true;
  } finally {
    if (own === epoch) busy.value = false;
  }
}
async function restore() {
  if (props.disabled || busy.value || !revision.value) return;
  busy.value = true;
  failed.value = false;
  const own = ++epoch;
  try {
    const old = await catalog.read(props.artifact.pipeline_id, revision.value);
    const result = await catalog.save(
      props.artifact.pipeline_id,
      props.branch === "main"
        ? old.definition
        : (old.authoring?.branches[props.branch]?.definition ?? old.definition),
      props.artifact.head_revision_id,
      "Restore reviewed revision",
      props.branch,
    );
    if (own === epoch) emit("changed", result);
  } catch {
    if (own === epoch) failed.value = true;
  } finally {
    if (own === epoch) busy.value = false;
  }
}
watch(source, () => {
  preview.value = undefined;
  conflicts.value = [];
  choices.value = {};
});
watch(
  () => props.artifact.head_revision_id,
  () => {
    preview.value = undefined;
    if (!props.artifact.authoring?.branches[source.value])
      source.value =
        Object.keys(props.artifact.authoring?.branches ?? {})[0] ?? "";
  },
);
onBeforeUnmount(() => {
  epoch++;
  catalog.cancel();
});
</script>
<template>
  <section
    :aria-label="$t('pipeline.collaboration.title')"
    class="space-y-4 rounded-panel border border-border bg-surface p-4"
  >
    <h2 class="text-lg font-semibold">
      {{
        $t(
          mode === "history"
            ? "pipeline.chrome.history"
            : "pipeline.chrome.proposals",
        )
      }}
    </h2>
    <UiAlert v-if="failed" tone="danger">{{
      $t("pipeline.collaboration.failed")
    }}</UiAlert>
    <p v-if="disabled" class="text-sm text-muted">
      {{ $t("pipeline.collaboration.saveFirst") }}
    </p>
    <div v-if="mode !== 'history'" class="flex flex-wrap items-end gap-3">
      <UiInput
        v-model="branchName"
        :label="$t('pipeline.collaboration.newBranch')"
        maxlength="128"
      /><UiButton
        variant="secondary"
        :disabled="
          disabled ||
          busy ||
          !/^[A-Za-z0-9_-]{1,128}$/.test(branchName) ||
          branchName === 'main'
        "
        :loading="busy"
        @click="
          command({
            operation: 'create_branch',
            branch: branchName,
            from_branch: branch,
          })
        "
        >{{ $t("pipeline.collaboration.create") }}</UiButton
      >
    </div>
    <ul
      v-if="
        mode !== 'history' &&
        Object.keys(artifact.authoring?.proposals ?? {}).length
      "
      class="divide-y divide-border border-y border-border"
    >
      <li
        v-for="(proposal, id) in artifact.authoring?.proposals"
        :key="id"
        class="flex flex-wrap items-center justify-between gap-3 py-3"
      >
        <div>
          <p class="text-sm font-medium">{{ proposal.source_branch }} → main</p>
          <p class="text-xs text-muted">
            {{ proposal.author_id }} ·
            {{ new Date(proposal.created_at_ms).toLocaleString() }}
          </p>
        </div>
        <UiButton
          variant="secondary"
          :disabled="busy"
          @click="
            source = String(id);
            review();
          "
          >{{ $t("pipeline.collaboration.review") }}</UiButton
        >
      </li>
    </ul>
    <UiEmptyState
      v-else-if="mode !== 'history'"
      :title="$t('pipeline.chrome.noProposals')"
      :description="$t('pipeline.chrome.proposalsHelp')"
    />
    <template
      v-if="
        mode !== 'history' &&
        Object.keys(artifact.authoring?.branches ?? {}).length
      "
    >
      <div class="flex flex-wrap items-end gap-3">
        <UiSelect
          v-model="source"
          :label="$t('pipeline.collaboration.sourceBranch')"
          :options="
            Object.keys(artifact.authoring?.branches ?? {}).map((value) => ({
              value,
              label: value,
            }))
          "
        /><UiButton
          :disabled="disabled || busy || !source"
          variant="secondary"
          @click="
            command({ operation: 'propose_merge', source_branch: source })
          "
          >{{ $t("pipeline.collaboration.propose") }}</UiButton
        ><UiButton
          :disabled="busy || !source"
          variant="secondary"
          @click="review()"
          >{{ $t("pipeline.collaboration.review") }}</UiButton
        >
      </div>
      <div v-if="preview" class="space-y-3">
        <p class="text-sm">{{ $t("pipeline.collaboration.previewHelp") }}</p>
        <PipelineConflictChoices
          v-model="choices"
          :conflicts="conflicts"
          @change="review(false)"
        />
        <details>
          <summary class="cursor-pointer text-sm">
            {{ $t("pipeline.collaboration.mergedPreview") }}
          </summary>
          <pre
            class="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all text-xs"
            >{{ JSON.stringify(preview.definition, null, 2) }}</pre>
        </details>
        <UiButton
          :disabled="
            disabled ||
            busy ||
            preview.conflicts.length > 0 ||
            !artifact.authoring?.proposals[source]
          "
          @click="
            command({
              operation: 'merge',
              proposal_id: source,
              resolutions: choices,
            })
          "
          >{{ $t("pipeline.collaboration.merge") }}</UiButton
        >
      </div>
    </template>
    <details v-if="mode === 'history'" open>
      <summary class="cursor-pointer text-sm font-medium">
        {{ $t("pipeline.library.version") }}
      </summary>
      <PipelineHistory
        :pipeline-id="artifact.pipeline_id"
        :revision="artifact.head_revision_id"
        class="mt-3"
        @select="compare"
      />
      <div v-if="changes" class="mt-4 space-y-3">
        <h3 class="text-sm font-medium">
          {{ $t("pipeline.collaboration.comparison") }}
        </h3>
        <p v-if="!changes.length" class="text-sm text-muted">
          {{ $t("pipeline.collaboration.noChanges") }}
        </p>
        <ul class="max-h-80 space-y-2 overflow-auto">
          <li v-for="change in changes" :key="change.path" class="text-sm">
            <details>
              <summary class="cursor-pointer break-all">
                {{ change.path }}
              </summary>
              <pre
                class="max-h-48 overflow-auto whitespace-pre-wrap break-all text-xs"
                >{{
                  JSON.stringify(
                    { before: change.before, after: change.after },
                    null,
                    2,
                  )
                }}</pre>
            </details>
          </li>
        </ul>
        <UiButton
          variant="secondary"
          :disabled="disabled || busy"
          @click="restore"
          >{{ $t("pipeline.collaboration.restore") }}</UiButton
        >
      </div>
    </details>
  </section>
</template>
