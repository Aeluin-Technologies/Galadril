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
import { documentScore, indexingState } from "~/utils/pengolo";
const props = defineProps<{ projectId?: string | null; picker?: boolean }>();
const emit = defineEmits<{ select: [file: FileRecord] }>();
const library = usePengoloLibrary();
const ui = usePengoloUi();
const projects = usePengoloProjects();
const account = useState<{ subject: string; tenant: string } | null>(
  "account-session",
);
const active = ref(true);
const documentVisibility = useDocumentVisibility();
const progressUnavailable = ref(false);
const indexing = computed(
  () =>
    new Map(library.items.map((file) => [file.id, indexingState(file.data)])),
);
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
const deleting = ref<FileRecord | null>(null);
const busy = ref(false);
const preview = ref<{ url: string; file: FileRecord } | null>(null);
watch([preview, () => library.items], ([selected, files]) => {
  if (selected && !files.some((file) => file.id === selected.file.id))
    preview.value = null;
});
watch([deleting, () => library.items], ([selected, files]) => {
  if (selected && !files.some((file) => file.id === selected.id))
    deleting.value = null;
});
onDeactivated(() => {
  active.value = false;
  preview.value = null;
  deleting.value = null;
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
    deleting.value = null;
    search.value = "";
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
async function remove() {
  if (!deleting.value) return;
  busy.value = true;
  try {
    await library.remove(deleting.value.id);
    deleting.value = null;
  } catch {
    ui.error = "The file could not be deleted. Please retry.";
  } finally {
    busy.value = false;
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
      <span aria-hidden="true">▱</span>
      <h3>No files yet</h3>
      <p>Keep documents here to use them again in Pengolo.</p>
    </div>
    <ul v-else class="p-file-list">
      <li v-for="file in library.items" :key="file.id">
        <div class="p-file-symbol" aria-hidden="true">▤</div>
        <div class="p-file-info">
          <strong>{{ file.data.name }}</strong
          ><small
            >{{ (file.data.size / 1024).toFixed(0) }} KB ·
            {{ file.data.status }} · expires
            {{
              file.expiresAt
                ? new Date(file.expiresAt).toLocaleDateString()
                : ""
            }}</small
          >
          <div
            class="p-index-progress"
            v-if="indexing.get(file.id)"
            :aria-label="`Indexing ${file.data.name}`"
          >
            <small
              >{{ indexing.get(file.id)?.label }} ·
              {{ indexing.get(file.id)?.percent }}%
              <span v-if="indexing.get(file.id)?.active">(approx.)</span></small
            >
            <small v-if="indexing.get(file.id)?.reasonKey" role="status">
              {{ $t(indexing.get(file.id)?.reasonKey ?? "") }}
            </small>
            <progress
              :value="indexing.get(file.id)?.percent"
              max="100"
              :aria-label="`Indexing progress for ${file.data.name}`"
            />
          </div>
          <UiButton
            v-if="!picker && file.ownerId === account?.subject"
            variant="ghost"
            type="button"
            @click="
              library
                .index(file.id)
                .catch(() => (ui.error = 'Indexing could not be requested.'))
            "
            >Refresh search index</UiButton
          >
        </div>
        <UiButton
          variant="secondary"
          v-if="picker"
          type="button"
          :disabled="file.data.status !== 'ready'"
          @click="emit('select', file)"
        >
          Attach</UiButton
        ><template v-else
          ><UiButton
            variant="ghost"
            type="button"
            :aria-label="`Preview ${file.data.name}`"
            :disabled="file.data.status !== 'ready'"
            @click="showPreview(file)"
          >
            ↗</UiButton
          ><UiButton
            variant="ghost"
            type="button"
            :aria-label="`Download ${file.data.name}`"
            :disabled="file.data.status !== 'ready'"
            @click="download(file)"
          >
            ↓</UiButton
          ><UiButton
            variant="ghost"
            type="button"
            :aria-label="`Delete ${file.data.name}`"
            @click="deleting = file"
          >
            ×
          </UiButton></template
        >
      </li>
    </ul>
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
      :open="!!deleting"
      title="Delete this file?"
      @close="deleting = null"
      ><p>
        {{ deleting?.data.name }} will no longer be available in this library or
        to Pengolo.
      </p>
      <template #footer
        ><UiButton variant="secondary" type="button" @click="deleting = null">
          Keep file</UiButton
        ><UiButton
          variant="danger"
          type="button"
          @click="remove"
          :loading="busy"
        >
          Delete file
        </UiButton></template
      ></UiDialog
    >
  </section>
</template>

<style scoped>
.p-index-progress {
  display: grid;
  gap: 0.25rem;
  max-width: 22rem;
}
.p-index-progress progress {
  width: 100%;
  height: 0.4rem;
  accent-color: var(--p-accent, #b96713);
}
</style>
