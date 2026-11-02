<script setup lang="ts">
import type { AuthoringCommand, PipelineArtifact } from "~/utils/pipeline";
const props = defineProps<{
  artifact: PipelineArtifact;
  branch: string;
  disabled: boolean;
  mode?: "proposals" | "history";
}>();
const emit = defineEmits<{
  changed: [artifact: PipelineArtifact];
  checkout: [branch: string];
}>();
const catalog = usePipelineCatalog();
const creating = ref(false),
  branchName = ref(""),
  parent = ref(props.branch);
const busy = ref(false),
  failed = ref(false);
let epoch = 0;
async function command(value: AuthoringCommand) {
  if (busy.value || props.disabled) return;
  const request = ++epoch;
  busy.value = true;
  failed.value = false;
  try {
    const result = await catalog.author(
      props.artifact.pipeline_id,
      props.artifact.definition,
      value,
      props.artifact.head_revision_id,
    );
    if (request !== epoch) return;
    emit("changed", result);
    if (value.operation === "create_branch") {
      creating.value = false;
      branchName.value = "";
      emit("checkout", value.branch);
    }
  } catch {
    if (request === epoch) failed.value = true;
  } finally {
    if (request === epoch) busy.value = false;
  }
}
function revision(value: string) {
  void navigateTo({
    path: `/pipeline/${encodeURIComponent(props.artifact.pipeline_id)}/revisions/${encodeURIComponent(value)}`,
    query: { branch: props.branch },
  });
}
onBeforeUnmount(() => {
  epoch++;
  catalog.cancel();
});
</script>
<template>
  <section
    class="mx-auto max-w-6xl space-y-5"
    :aria-label="$t('pipeline.collaboration.title')"
  >
    <UiAlert v-if="failed" tone="danger">{{
      $t("pipeline.collaboration.failed")
    }}</UiAlert>
    <p v-if="disabled" class="text-sm text-muted">
      {{ $t("pipeline.collaboration.saveFirst") }}
    </p>
    <template v-if="mode === 'history'">
      <UiPageHeader
        :title="$t('pipeline.chrome.history')"
        :description="$t('pipeline.review.historyHelp')"
      />
      <PipelineHistory
        :pipeline-id="artifact.pipeline_id"
        :revision="artifact.head_revision_id"
        @select="revision"
      />
    </template>
    <div v-else class="grid gap-6 lg:grid-cols-[280px_minmax(0,1fr)]">
      <aside
        class="space-y-4 rounded-panel border border-border bg-surface p-4"
      >
        <h2 class="font-semibold">{{ $t("pipeline.library.branches") }}</h2>
        <p class="text-sm text-muted">{{ $t("pipeline.review.branchHelp") }}</p>
        <PipelineBranchTree
          :branches="artifact.authoring?.branches ?? {}"
          :selected="branch"
          :disabled="disabled || busy"
          @checkout="emit('checkout', $event)"
        />
        <UiButton
          variant="secondary"
          :disabled="disabled || busy"
          @click="
            parent = branch;
            creating = true;
          "
          >{{ $t("pipeline.collaboration.create") }}</UiButton
        >
      </aside>
      <div class="min-w-0 space-y-4">
        <UiPageHeader
          :title="$t('pipeline.chrome.proposals')"
          :description="$t('pipeline.review.proposalHelp')"
        >
          <template #actions
            ><UiButton
              v-if="branch !== 'main'"
              :disabled="disabled || busy"
              :loading="busy"
              @click="
                command({ operation: 'propose_merge', source_branch: branch })
              "
              >{{ $t("pipeline.collaboration.propose") }}</UiButton
            ></template
          >
        </UiPageHeader>
        <ul
          v-if="Object.keys(artifact.authoring?.proposals ?? {}).length"
          class="divide-y divide-border rounded-panel border border-border bg-surface"
        >
          <li
            v-for="(proposal, id) in artifact.authoring?.proposals"
            :key="id"
            class="flex flex-wrap items-center justify-between gap-3 p-4"
          >
            <div>
              <NuxtLink
                :to="`/pipeline/${encodeURIComponent(artifact.pipeline_id)}/proposals/${encodeURIComponent(id)}`"
                class="font-medium text-primary-foreground hover:underline"
                >{{ proposal.source_branch }} → main</NuxtLink
              >
              <p class="mt-1 text-xs text-muted">
                {{ proposal.author_id }} ·
                {{ new Date(proposal.created_at_ms).toLocaleString() }}
              </p>
            </div>
            <NuxtLink
              :to="`/pipeline/${encodeURIComponent(artifact.pipeline_id)}/proposals/${encodeURIComponent(id)}`"
              class="text-sm text-primary-foreground underline underline-offset-4"
              >{{ $t("pipeline.collaboration.review") }}</NuxtLink
            >
          </li>
        </ul>
        <UiEmptyState
          v-else
          :title="$t('pipeline.chrome.noProposals')"
          :description="$t('pipeline.chrome.proposalsHelp')"
        />
      </div>
    </div>
    <UiDialog
      v-model:open="creating"
      :title="$t('pipeline.collaboration.create')"
    >
      <p class="mb-4 text-sm text-muted">
        {{ $t("pipeline.review.branchHelp") }}
      </p>
      <div class="space-y-4">
        <UiSelect
          v-model="parent"
          :label="$t('pipeline.review.fromBranch')"
          :options="
            ['main', ...Object.keys(artifact.authoring?.branches ?? {})].map(
              (value) => ({ value, label: value }),
            )
          "
        /><UiInput
          v-model="branchName"
          :label="$t('pipeline.collaboration.newBranch')"
          maxlength="128"
        />
      </div>
      <template #footer
        ><UiButton variant="secondary" @click="creating = false">{{
          $t("pipeline.library.cancel")
        }}</UiButton
        ><UiButton
          :disabled="
            disabled ||
            busy ||
            !/^[A-Za-z0-9_-]{1,128}$/.test(branchName) ||
            branchName === 'main' ||
            !!artifact.authoring?.branches[branchName]
          "
          :loading="busy"
          @click="
            command({
              operation: 'create_branch',
              branch: branchName,
              from_branch: parent,
            })
          "
          >{{ $t("pipeline.review.createAndEdit") }}</UiButton
        ></template
      >
    </UiDialog>
  </section>
</template>
