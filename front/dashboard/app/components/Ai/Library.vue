<script setup lang="ts">
import {
  useDebounceFn,
  useEventListener,
  useIntervalFn,
  useDocumentVisibility,
} from "@vueuse/core";
import { usePengoloLibrary } from "~/stores/usePengoloLibrary";
import { usePengoloProjects } from "~/stores/usePengoloProjects";
import { usePengoloUi } from "~/stores/usePengoloUi";
import type { FileRecord } from "~/utils/pengolo-api";
import {
  FolderOpenIcon,
  ArrowDownTrayIcon,
  TrashIcon,
  ChatBubbleLeftRightIcon,
  XMarkIcon,
} from "@heroicons/vue/24/outline";
import { documentScore } from "~/utils/pengolo";
const props = defineProps<{ projectId?: string | null; picker?: boolean }>();
const emit = defineEmits<{
  select: [file: FileRecord];
  startChat: [files: FileRecord[]];
}>();
const { t } = useI18n();
const library = usePengoloLibrary();
const ui = usePengoloUi();
const projects = usePengoloProjects();
const account = useState<{ subject: string; tenant: string } | null>(
  "account-session",
);
const active = ref(true);
const documentVisibility = useDocumentVisibility();
const progressUnavailable = ref(false);
useIntervalFn(async () => {
  if (
    !active.value ||
    !account.value ||
    !visible.value ||
    documentVisibility.value !== "visible"
  )
    return;
  try {
    await library.refreshIndexing();
    progressUnavailable.value = false;
  } catch {
    progressUnavailable.value = true;
  }
}, 3000);
const visible = computed(
  () =>
    !props.projectId ||
    projects.items.some((project) => project.id === props.projectId),
);
const search = ref("");
const searchMode = ref("hybrid");
const mime = ref("");
const minSimilarity = ref(0.3);
const minRelevance = ref(0);
const selection = useDocumentSelection(
  () =>
    `${props.projectId ?? ""}/${account.value?.tenant}/${account.value?.subject}`,
);
const {
  selected,
  deleting,
  busy,
  files: selectedFiles,
  all,
  ready,
} = selection;
const canDelete = (file: FileRecord) =>
  file.ownerId === account.value?.subject ||
  projects.items.some(
    (project) =>
      project.id === file.projectId &&
      project.ownerId === account.value?.subject,
  );
const canDeleteSelected = computed(
  () => selectedFiles.value.length > 0 && selectedFiles.value.every(canDelete),
);
function startChat() {
  if (selectedFiles.value.length > 8 || !ready.value) {
    ui.error = t("documents.chatLimit");
    return;
  }
  emit("startChat", [...selectedFiles.value]);
}
async function reindex(file: FileRecord) {
  try {
    await library.index(file.id);
  } catch {
    ui.error = t("documents.reindexError");
  }
}
const preview = ref<{ url: string; file: FileRecord } | null>(null);
watch([preview, () => library.items], ([selected, files]) => {
  if (selected && !files.some((file) => file.id === selected.file.id))
    preview.value = null;
});
onDeactivated(() => {
  active.value = false;
  preview.value = null;
  deleting.value = [];
});
async function showPreview(file: FileRecord) {
  try {
    const result = await library.download(file.id);
    if (!library.items.some((item) => item.id === file.id)) return;
    const url = new URL(result.url, window.location.origin);
    url.searchParams.set("preview", "true");
    preview.value = { url: url.href, file };
  } catch {
    ui.error = "This document is no longer available.";
  }
}
const fileInput = ref<HTMLInputElement | null>(null);
const refresh = async () => {
  if (!active.value || !account.value || !visible.value) return;
  try {
    if (search.value.trim() && searchMode.value !== "filename") {
      await library.searchContents({
        query: search.value,
        projectId: props.projectId ?? null,
        mode: searchMode.value,
        mimeTypes: mime.value ? [mime.value] : [],
        minSimilarity: minSimilarity.value,
        minRelevance: minRelevance.value,
      });
    } else {
      await library.load(props.projectId ?? null, search.value);
    }
  } catch {
    ui.error = "Your library is unavailable. Check your access and try again.";
  }
};
onActivated(() => {
  active.value = true;
  void refresh();
});
useEventListener("pengolo-identity-verified", () => void refresh());
const delayedSearch = useDebounceFn(refresh, 250);
watch(
  [search, searchMode, mime, minSimilarity, minRelevance],
  () => void delayedSearch(),
);
watch(
  [
    () => props.projectId,
    () => account.value?.tenant,
    () => account.value?.subject,
    visible,
  ],
  () => {
    preview.value = null;
    deleting.value = [];
    search.value = "";
    if (!visible.value) library.clear();
    void refresh();
  },
  { immediate: true },
);
async function upload(event: Event) {
  const input = event.target as HTMLInputElement;
  for (const file of Array.from(input.files ?? [])) {
    try {
      await library.upload(file, props.projectId ?? null);
    } catch {
      ui.error =
        "Upload failed. Supported files: PDF, text, CSV, PNG, JPEG and WebP, up to 25 MB.";
    }
  }
  input.value = "";
}
async function download(file: FileRecord) {
  try {
    const result = await library.download(file.id);
    const link = document.createElement("a");
    link.href = result.url;
    link.rel = "noopener noreferrer";
    link.download = file.data.name;
    link.click();
  } catch {
    ui.error = "This document is no longer available.";
  }
}
</script>

<template>
  <section class="p-library" aria-label="File library">
    <p v-if="progressUnavailable" class="p-muted" role="status">
      Progress updates are temporarily unavailable. The last confirmed stage is
      shown.
    </p>
    <div class="p-library-toolbar">
      <label class="p-search"
        ><span class="sr-only">Search library</span
        ><UiInput
          v-model="search"
          type="search"
          placeholder="Search your files…" /></label
      ><UiButton
        variant="secondary"
        v-if="!picker"
        type="button"
        :disabled="library.uploading > 0"
        @click="fileInput?.click()"
      >
        {{ library.uploading ? "Uploading…" : "Upload files" }}</UiButton
      ><input
        ref="fileInput"
        class="sr-only"
        type="file"
        multiple
        :aria-label="$t('documents.upload')"
        accept=".pdf,.txt,.csv,.png,.jpg,.jpeg,.webp"
        @change="upload"
      />
    </div>
    <details class="p-muted">
      <summary>Search options</summary>
      <label
        >Search by
        <UiSelect
          v-model="searchMode"
          :options="[
            { label: 'Words and meaning'.trim(), value: 'hybrid' },
            { label: 'Exact words'.trim(), value: 'lexical' },
            { label: 'Meaning'.trim(), value: 'semantic' },
            { label: 'Connected evidence'.trim(), value: 'hipporag2' },
            { label: 'Filename'.trim(), value: 'filename' },
          ]"
      /></label>
      <label
        >File type
        <UiSelect
          v-model="mime"
          :options="[
            { label: 'All types'.trim(), value: '' },
            { label: 'PDF'.trim(), value: 'application/pdf' },
            { label: 'Text'.trim(), value: 'text/plain' },
            { label: 'CSV'.trim(), value: 'text/csv' },
          ]"
      /></label>
      <label
        >Minimum similarity
        <UiInput
          v-model.number="minSimilarity"
          type="number"
          min="0"
          max="1"
          step="0.05"
      /></label>
      <label
        >Minimum relevance
        <UiInput
          v-model.number="minRelevance"
          type="number"
          min="0"
          max="1"
          step="0.05"
      /></label>
      <p>
        Scores compare the query with evidence. They do not measure factual
        correctness. Relevance filtering requires the reranker.
      </p>
    </details>
    <p class="p-muted">
      {{
        projectId ? "Only files from this project." : "Your personal library."
      }}
      Files expire 30 days after their last read.
    </p>
    <div
      v-if="library.loading"
      class="p-skeleton"
      role="status"
      aria-label="Loading files"
    />
    <div v-else-if="library.matches" aria-live="polite">
      <p v-if="library.matches.warnings.length" role="status" class="p-muted">
        Some search features are unavailable. Available results are shown.
      </p>
      <p v-if="!library.matches.hits.length">
        No matching passages. Files may still be indexing; try another query or
        search by filename.
      </p>
      <ul class="p-file-list">
        <li v-for="hit in library.matches.hits" :key="hit.unitId">
          <div class="p-file-info">
            <strong
              >{{ hit.filename
              }}<template v-if="hit.page">
                · page {{ hit.page }}</template
              ></strong
            >
            <p>{{ hit.text }}</p>
            <small v-if="documentScore(hit.similarity)"
              >Similarity {{ documentScore(hit.similarity) }}</small
            >
            <small v-if="documentScore(hit.relevance)">
              · Relevance {{ documentScore(hit.relevance) }}</small
            >
          </div>
          <UiButton
            v-if="picker"
            variant="secondary"
            type="button"
            @click="emit('select', hit.file)"
            >Attach</UiButton
          >
          <UiButton
            v-else
            variant="ghost"
            type="button"
            @click="showPreview(hit.file)"
            >Preview</UiButton
          >
        </li>
      </ul>
    </div>
    <div v-else-if="!library.items.length" class="p-empty">
      <FolderOpenIcon
        class="mx-auto mb-3 size-10 text-muted"
        aria-hidden="true"
      />
      <h3>{{ $t("documents.noFiles") }}</h3>
      <p>{{ $t("documents.emptyHelp") }}</p>
    </div>
    <div v-else>
      <div
        v-if="!picker"
        class="mt-5 flex items-center gap-3 border-b border-border px-3 py-3 text-xs text-muted"
      >
        <UiCheckbox
          :model-value="all"
          :indeterminate="selected.length > 0 && !all"
          :aria-label="$t('documents.selectAll')"
          :disabled="busy"
          @update:model-value="selection.toggleAll($event === true)"
        />
        <span>{{ $t("documents.name") }}</span>
      </div>
      <ul class="m-0 list-none p-0">
        <AiDocumentRow
          v-for="file in library.items"
          :key="file.id"
          :file="file"
          :selected="selected.includes(file.id)"
          :picker="picker"
          :busy="busy"
          :can-delete="canDelete(file)"
          :can-index="file.ownerId === account?.subject"
          @select="selection.toggle(file.id, $event)"
          @attach="emit('select', file)"
          @preview="showPreview(file)"
          @download="download(file)"
          @remove="deleting = [file]"
          @reindex="reindex(file)"
        />
      </ul>
    </div>
    <div
      v-if="!picker && selectedFiles.length"
      role="toolbar"
      :aria-label="$t('documents.selection')"
      class="sticky bottom-3 z-10 mx-auto flex w-fit max-w-full flex-wrap items-center justify-center gap-2 rounded-panel border border-border-control bg-surface p-3 shadow-popover"
    >
      <span class="px-2 text-sm font-medium" aria-live="polite">{{
        $t("documents.selected", { count: selectedFiles.length })
      }}</span>
      <UiButton
        :disabled="busy || !ready || selectedFiles.length > 8"
        @click="startChat"
        ><ChatBubbleLeftRightIcon aria-hidden="true" />{{
          $t("documents.startChat")
        }}</UiButton
      >
      <UiButton
        variant="secondary"
        :disabled="busy || !ready"
        :action="selection.download"
        ><ArrowDownTrayIcon aria-hidden="true" />{{
          $t("documents.downloadSelected")
        }}</UiButton
      >
      <UiButton
        variant="danger"
        :disabled="busy || !canDeleteSelected"
        @click="deleting = [...selectedFiles]"
        ><TrashIcon aria-hidden="true" />{{
          $t("documents.deleteSelected")
        }}</UiButton
      >
      <UiIconButton
        :label="$t('documents.clear')"
        :icon="XMarkIcon"
        :disabled="busy"
        @click="selected = []"
      />
    </div>
    <UiButton
      variant="secondary"
      v-if="library.after"
      type="button"
      :disabled="library.loading"
      @click="library.load(projectId ?? null, search, true)"
    >
      Load more files
    </UiButton>
    <UiDialog
      :open="!!preview"
      :title="preview?.file.data.name ?? 'Document preview'"
      @close="preview = null"
      ><img
        v-if="preview?.file.data.mime.startsWith('image/')"
        :src="preview.url"
        :alt="preview.file.data.name"
        style="max-width: 100%; max-height: 65vh" /><iframe
        v-else-if="preview"
        :src="preview.url"
        :title="preview.file.data.name"
        style="width: 100%; height: 65vh; border: 0"
        :sandbox="
          preview.file.data.mime === 'application/pdf' ? undefined : ''
        "
    /></UiDialog>
    <UiDialog
      :open="deleting.length > 0"
      :title="$t('documents.confirmDelete')"
      :dismissible="!busy"
      @close="deleting = []"
    >
      <p>{{ $t("documents.deleteExplanation", { count: deleting.length }) }}</p>
      <template #footer>
        <UiButton variant="secondary" :disabled="busy" @click="deleting = []">{{
          $t("documents.keep")
        }}</UiButton>
        <UiButton variant="danger" :loading="busy" @click="selection.remove"
          ><TrashIcon aria-hidden="true" />{{
            $t("documents.deleteConfirm")
          }}</UiButton
        >
      </template>
    </UiDialog>
  </section>
</template>
