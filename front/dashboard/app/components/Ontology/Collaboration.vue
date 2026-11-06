<script setup lang="ts">
import { TrashIcon, PlusIcon, ArrowPathIcon } from "@heroicons/vue/24/outline";
import type {
  OntologyWorkspace,
  OntologyCommand,
  OntologyHistoryPage,
  OntologyHistoryItem,
  SemanticDocument,
} from "~/types/ontology";
import { ontologyGraph } from "~/utils/ontology-graph";
const props = defineProps<{
  id: string;
  artifact: OntologyWorkspace;
  branch: string;
  mode: "branches" | "history";
  disabled: boolean;
  writable: boolean;
  error: string;
  command: (command: OntologyCommand) => Promise<boolean | undefined>;
}>();
const emit = defineEmits<{ checkout: [branch: string] }>();
const api = useOntologyCatalog();
const mutationFailed = ref(false);
const creating = ref(false),
  deleting = ref(""),
  merging = ref("");
const branchName = ref("");
const page = shallowRef<OntologyHistoryPage>();
const cursors = ref<Array<string | undefined>>([undefined]);
const loading = ref(false),
  failed = ref(false);
const historical = shallowRef<SemanticDocument>();
const { resourceLabel } = useOfficialOntologyLabels();
const preview = ref(false),
  previewMode = ref("graph"),
  selected = ref("");
const graph = computed(() =>
  ontologyGraph(historical.value, selected.value, (resource) =>
    resourceLabel(resource, historical.value?.plugin_origin?.package_id),
  ),
);
let epoch = 0;
async function history(cursor?: string) {
  const request = ++epoch;
  loading.value = true;
  failed.value = false;
  try {
    const result = await api.history(props.id, cursor);
    if (request === epoch) page.value = result;
  } catch {
    if (request === epoch) failed.value = true;
  } finally {
    if (request === epoch) loading.value = false;
  }
}
async function inspect(item: OntologyHistoryItem) {
  const request = ++epoch;
  loading.value = true;
  failed.value = false;
  try {
    const value = await api.workspace(props.id, item.revision_id);
    if (request !== epoch) return;
    historical.value =
      value.authoring.branches[item.branch]?.definition ?? value.ontology;
    selected.value = "";
    previewMode.value = "graph";
    preview.value = true;
  } catch {
    if (request === epoch) failed.value = true;
  } finally {
    if (request === epoch) loading.value = false;
  }
}
async function command(value: OntologyCommand) {
  mutationFailed.value = false;
  if (await props.command(value)) {
    creating.value = false;
    deleting.value = "";
    merging.value = "";
    branchName.value = "";
  } else {
    mutationFailed.value = true;
  }
}
async function next() {
  if (!page.value?.next_revision_id) return;
  cursors.value.push(page.value.next_revision_id);
  await history(page.value.next_revision_id);
}
async function previous() {
  if (cursors.value.length < 2) return;
  cursors.value.pop();
  await history(cursors.value.at(-1));
}
watch([creating, deleting, merging], () => {
  mutationFailed.value = false;
});
watch(
  () => [props.id, props.artifact.revision_id, props.mode],
  () => {
    epoch++;
    api.cancel();
    page.value = undefined;
    preview.value = false;
    cursors.value = [undefined];
    if (props.mode === "history") void history();
  },
  { immediate: true },
);
onBeforeUnmount(() => {
  epoch++;
  api.cancel();
});
</script>
<template>
  <section
    class="space-y-4"
    :aria-label="$t(`ontologyManager.collaboration.${mode}`)"
  >
    <p v-if="failed" role="alert" class="text-sm text-danger">
      {{ $t("ontologyManager.collaboration.failed") }}
      <UiButton variant="ghost" @click="history(cursors.at(-1))">{{
        $t("ontologyManager.collaboration.retry")
      }}</UiButton>
    </p>
    <p v-if="loading" role="status" class="text-sm text-muted">
      {{ $t("ontologyManager.loading") }}
    </p>
    <template v-if="mode === 'branches'">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <p class="max-w-xl text-sm text-muted">
          {{ $t("ontologyManager.collaboration.branchHelp") }}
        </p>
        <UiButton
          v-if="writable"
          variant="secondary"
          :disabled="disabled"
          @click="creating = true"
          ><PlusIcon class="mr-2 h-4 w-4" aria-hidden="true" />{{
            $t("ontologyManager.collaboration.newBranch")
          }}</UiButton
        >
      </div>
      <div
        class="divide-y divide-border rounded-panel border border-border bg-surface"
      >
        <div class="flex items-center gap-3 p-4">
          <UiButton
            variant="ghost"
            :aria-pressed="branch === 'main'"
            :disabled="disabled"
            @click="emit('checkout', 'main')"
            >main</UiButton
          ><span class="text-xs text-muted">{{
            $t("ontologyManager.collaboration.main")
          }}</span>
        </div>
        <div
          v-for="(item, key) in artifact.authoring.branches"
          :key="key"
          class="flex flex-wrap items-center gap-3 p-4"
        >
          <UiButton
            variant="ghost"
            :aria-pressed="branch === key"
            :disabled="disabled"
            @click="emit('checkout', key)"
            >{{ key }}</UiButton
          >
          <span class="min-w-0 flex-1 text-xs text-muted">{{
            $t("ontologyManager.collaboration.parent", {
              branch: item.parent_branch,
            })
          }}</span>
          <UiButton
            v-if="writable"
            variant="secondary"
            :disabled="disabled"
            @click="merging = key"
            >{{
              $t("ontologyManager.collaboration.merge", {
                branch: item.parent_branch,
              })
            }}</UiButton
          >
          <UiIconButton
            v-if="writable"
            :icon="TrashIcon"
            :label="$t('pipeline.review.deleteBranch')"
            :disabled="
              disabled ||
              Object.values(artifact.authoring.branches).some(
                (child) => child.parent_branch === key,
              )
            "
            @click="deleting = key"
          />
        </div>
      </div>
    </template>
    <template v-else>
      <p class="text-sm text-muted">
        {{ $t("ontologyManager.collaboration.historyHelp") }}
      </p>
      <div
        v-if="page?.items.length"
        class="divide-y divide-border rounded-panel border border-border bg-surface"
      >
        <div
          v-for="item in page.items"
          :key="item.revision_id"
          class="flex flex-wrap items-center justify-between gap-2 p-4"
        >
          <div class="min-w-0">
            <UiButton
              variant="ghost"
              align="start"
              :disabled="loading"
              @click="inspect(item)"
              >{{ item.message || item.operation }}</UiButton
            >
            <p class="break-all text-xs text-muted">
              {{ item.branch }} · {{ item.author }} ·
              {{ new Date(item.created_at_ms).toLocaleString() }}
            </p>
            <code class="text-xs text-muted">{{
              item.revision_id.slice(0, 12)
            }}</code>
          </div>
        </div>
      </div>
      <p v-else-if="!loading && !failed" class="text-sm text-muted">
        {{ $t("ontologyManager.collaboration.emptyHistory") }}
      </p>
      <div class="flex gap-2">
        <UiButton
          variant="secondary"
          :disabled="loading || cursors.length < 2"
          @click="previous"
          >{{ $t("ontologyManager.collaboration.previous") }}</UiButton
        ><UiButton
          variant="secondary"
          :disabled="loading || !page?.next_revision_id"
          @click="next"
          >{{ $t("ontologyManager.collaboration.next") }}</UiButton
        ><UiIconButton
          :icon="ArrowPathIcon"
          :label="$t('ontologyManager.collaboration.retry')"
          :disabled="loading"
          @click="history(cursors.at(-1))"
        />
      </div>
    </template>
    <UiDialog
      v-model:open="creating"
      :title="$t('ontologyManager.collaboration.createBranch')"
      :description="$t('ontologyManager.collaboration.branchHelp')"
    >
      <UiField
        :label="$t('ontologyManager.collaboration.branchName')"
        v-slot="field"
        ><UiInput v-model="branchName" :id="field.id" :disabled="disabled"
      /></UiField>
      <template #footer
        ><UiButton variant="secondary" @click="creating = false">{{
          $t("ontologyManager.cancel")
        }}</UiButton
        ><UiButton
          :disabled="
            disabled ||
            !/^[A-Za-z0-9_-]{1,128}$/.test(branchName) ||
            branchName === 'main' ||
            !!artifact.authoring.branches[branchName]
          "
          @click="
            command({
              operation: 'create_branch',
              branch: branchName,
              from_branch: branch,
            })
          "
          >{{ $t("ontologyManager.collaboration.createBranch") }}</UiButton
        ></template
      >
    </UiDialog>
    <UiDialog
      :open="!!deleting"
      :title="$t('pipeline.review.deleteBranch')"
      :description="
        $t('ontologyManager.collaboration.deleteBranchHelp', {
          branch: deleting,
        })
      "
      @update:open="
        (value) => {
          if (!value) deleting = '';
        }
      "
    >
      <template #footer
        ><UiButton variant="secondary" @click="deleting = ''">{{
          $t("ontologyManager.cancel")
        }}</UiButton
        ><UiButton
          variant="danger"
          :disabled="disabled"
          @click="command({ operation: 'delete_branch', branch: deleting })"
          >{{ $t("pipeline.review.deleteBranch") }}</UiButton
        ></template
      >
    </UiDialog>
    <UiDialog
      :open="!!merging"
      :title="$t('ontologyManager.collaboration.mergeTitle')"
      :description="$t('ontologyManager.collaboration.mergeHelp')"
      @update:open="
        (value) => {
          if (!value) merging = '';
        }
      "
    >
      <template #footer
        ><UiButton variant="secondary" @click="merging = ''">{{
          $t("ontologyManager.cancel")
        }}</UiButton
        ><UiButton
          :disabled="disabled"
          @click="command({ operation: 'merge', branch: merging })"
          >{{ $t("ontologyManager.collaboration.mergeTitle") }}</UiButton
        ></template
      >
    </UiDialog>
    <UiDialog
      v-model:open="preview"
      :title="$t('ontologyManager.collaboration.revision')"
      :description="$t('ontologyManager.collaboration.historyHelp')"
      size="wide"
    >
      <template v-if="historical"
        ><nav class="mb-3 flex gap-2">
          <UiButton
            v-for="view in ['graph', 'code']"
            :key="view"
            variant="ghost"
            :aria-pressed="previewMode === view"
            @click="previewMode = view"
            >{{ $t(`pipeline.review.${view}`) }}</UiButton
          >
        </nav>
        <LazyOntologyRelations
          v-if="previewMode === 'graph'"
          :nodes="graph.nodes"
          :edges="graph.edges"
          @select="
            (value) => (selected = selected === value ? '' : value)
          " /><OntologyCode
          v-else
          :document="historical"
          :editable="false"
          :selected="selected"
          :active="true"
      /></template>
    </UiDialog>
  </section>
</template>
