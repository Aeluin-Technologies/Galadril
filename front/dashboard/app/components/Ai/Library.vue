<script setup lang="ts">
import { useDebounceFn, useEventListener } from "@vueuse/core";
import { usePengoloLibrary } from "~/stores/usePengoloLibrary";
import { usePengoloProjects } from "~/stores/usePengoloProjects";
import { usePengoloUi } from "~/stores/usePengoloUi";
import type { FileRecord } from "~/utils/pengolo-api";
const props = defineProps<{ projectId?: string | null; picker?: boolean }>();
const emit = defineEmits<{ select: [file: FileRecord] }>();
const library = usePengoloLibrary();
const ui = usePengoloUi();
const projects = usePengoloProjects();
const account = useState<{ subject: string; tenant: string } | null>(
  "account-session",
);
const active = ref(true);
const visible = computed(
  () =>
    !props.projectId ||
    projects.items.some((project) => project.id === props.projectId),
);
const search = ref("");
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
    await library.load(props.projectId ?? null, search.value);
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
watch(search, () => void delayedSearch());
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
    <div class="p-library-toolbar">
      <label class="p-search"
        ><span class="sr-only">Search library</span
        ><input
          v-model="search"
          type="search"
          placeholder="Search your files…" /></label
      ><UtilsButton
        variant="secondary"
        v-if="!picker"
        type="button"
        class="p-button"
        :disabled="library.uploading > 0"
        @click="fileInput?.click()"
      >
        {{ library.uploading ? "Uploading…" : "Upload files" }}</UtilsButton
      ><input
        ref="fileInput"
        class="sr-only"
        type="file"
        multiple
        accept=".pdf,.txt,.csv,.png,.jpg,.jpeg,.webp"
        @change="upload"
      />
    </div>
    <p class="p-muted">
      {{
        projectId ? "Only files from this project." : "Your personal library."
      }}
      Files expire 30 days after upload.
    </p>
    <div
      v-if="library.loading"
      class="p-skeleton"
      role="status"
      aria-label="Loading files"
    />
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
        </div>
        <UtilsButton
          variant="secondary"
          v-if="picker"
          type="button"
          class="p-button"
          :disabled="file.data.status !== 'ready'"
          @click="emit('select', file)"
        >
          Attach</UtilsButton
        ><template v-else
          ><UtilsButton
            variant="ghost"
            type="button"
            class="p-icon"
            :aria-label="`Preview ${file.data.name}`"
            :disabled="file.data.status !== 'ready'"
            @click="showPreview(file)"
          >
            ↗</UtilsButton
          ><UtilsButton
            variant="ghost"
            type="button"
            class="p-icon"
            :aria-label="`Download ${file.data.name}`"
            :disabled="file.data.status !== 'ready'"
            @click="download(file)"
          >
            ↓</UtilsButton
          ><UtilsButton
            variant="ghost"
            type="button"
            class="p-icon danger"
            :aria-label="`Delete ${file.data.name}`"
            @click="deleting = file"
          >
            ×
          </UtilsButton></template
        >
      </li>
    </ul>
    <UtilsButton
      variant="secondary"
      v-if="library.after"
      type="button"
      class="p-button"
      :disabled="library.loading"
      @click="library.load(projectId ?? null, search, true)"
    >
      Load more files
    </UtilsButton>
    <AiDialog
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
        sandbox=""
    /></AiDialog>
    <AiDialog
      :open="!!deleting"
      title="Delete this file?"
      @close="deleting = null"
      ><p>
        {{ deleting?.data.name }} will no longer be available in this library or
        to Pengolo.
      </p>
      <template #footer
        ><UtilsButton
          variant="secondary"
          type="button"
          class="p-button"
          @click="deleting = null"
        >
          Keep file</UtilsButton
        ><UtilsButton
          variant="danger"
          type="button"
          class="p-button danger"
          :disabled="busy"
          @click="remove"
        >
          Delete file
        </UtilsButton></template
      ></AiDialog
    >
  </section>
</template>
