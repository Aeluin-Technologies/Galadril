import { defineStore } from "pinia";
import { createRequestCache, indexingState } from "~/utils/pengolo";
import {
  pengoloMutation,
  pengoloRequest,
  pengoloOperation,
  type FileRecord,
  type DocumentPage,
} from "~/utils/pengolo-api";

export const usePengoloLibrary = defineStore("pengoloLibrary", () => {
  const items = ref<FileRecord[]>([]);
  const loading = ref(false);
  const uploading = ref(0);
  const after = ref<string | null>(null);
  const matches = ref<DocumentPage | null>(null);
  const cache = createRequestCache<{
    items: FileRecord[];
    after: string | null;
  }>();
  let selection = "";
  let loadEpoch = 0;
  let identityEpoch = 0;
  let polling = false;
  let progressEpoch = 0;
  let progressCursor = 0;
  let selectedProject: string | null = null;
  let selectedSearch = "";
  async function load(
    projectId: string | null = null,
    search = "",
    more = false,
  ) {
    const epoch = ++loadEpoch;
    selection = JSON.stringify([projectId, search]);
    selectedProject = projectId;
    selectedSearch = search;
    loading.value = true;
    matches.value = null;
    try {
      const variables = {
        project: projectId,
        search,
        after: more ? after.value : null,
      };
      const page = await cache.get(JSON.stringify(variables), () =>
        pengoloRequest<{ items: FileRecord[]; after: string | null }>(
          pengoloOperation("Library"),
          variables,
        ),
      );
      if (epoch !== loadEpoch) return;
      items.value = more
        ? [
            ...items.value,
            ...page.items.filter(
              (item) => !items.value.some((old) => old.id === item.id),
            ),
          ]
        : page.items;
      after.value = page.after;
    } finally {
      if (epoch === loadEpoch) loading.value = false;
    }
  }
  async function searchContents(input: {
    query: string;
    projectId: string | null;
    mode: string;
    mimeTypes: string[];
    minSimilarity: number;
    minRelevance: number;
  }) {
    const epoch = ++loadEpoch;
    matches.value = null;
    items.value = [];
    after.value = null;
    loading.value = true;
    try {
      const page = await pengoloRequest<DocumentPage>(
        pengoloOperation("DocumentSearch"),
        { input },
      );
      if (epoch !== loadEpoch) return;
      matches.value = page;
      items.value = Array.from(
        new Map(page.hits.map((hit) => [hit.file.id, hit.file])).values(),
      );
    } finally {
      if (epoch === loadEpoch) loading.value = false;
    }
  }
  async function index(id: string) {
    const identity = identityEpoch;
    await pengoloRequest<boolean>(pengoloOperation("IndexDocument"), { id });
    if (identity !== identityEpoch) return;
    progressEpoch++;
    cache.clear();
    const file = items.value.find((item) => item.id === id);
    if (file) file.data.indexing = { stage: "queued", percent: 0 };
  }
  async function refreshIndexing() {
    if (polling || loading.value || matches.value) return;
    const snapshot = items.value;
    const pending = snapshot.flatMap((file, index) =>
      indexingState(file.data).active ? [index] : [],
    );
    if (!pending.length) return;
    const position = progressCursor % pending.length;
    const offset = pending[position];
    if (offset === undefined) return;
    const epoch = loadEpoch;
    const progress = progressEpoch;
    const identity = identityEpoch;
    polling = true;
    try {
      // One authorized page per tick bounds work even with many loaded pages.
      const page = await pengoloRequest<{
        items: FileRecord[];
        after: string | null;
      }>(pengoloOperation("Library"), {
        project: selectedProject,
        search: selectedSearch,
        after: snapshot[offset - 1]?.id ?? null,
      });
      if (
        epoch !== loadEpoch ||
        progress !== progressEpoch ||
        identity !== identityEpoch
      )
        return;
      const current = new Map(page.items.map((file) => [file.id, file]));
      const covered = pending.filter((index) =>
        current.has(snapshot[index]!.id),
      ).length;
      progressCursor = position + Math.max(1, covered);
      items.value = items.value.map((file) => current.get(file.id) ?? file);
      cache.clear();
    } finally {
      polling = false;
    }
  }
  async function upload(
    file: File,
    projectId: string | null,
    temporarySession: string | null = null,
  ) {
    const epoch = identityEpoch;
    const current = () => {
      if (epoch !== identityEpoch)
        throw new Error("Workspace identity changed");
    };
    uploading.value++;
    try {
      const digest = new Uint8Array(
        await crypto.subtle.digest("SHA-256", await file.arrayBuffer()),
      );
      current();
      const checksum = btoa(String.fromCharCode(...digest));
      const result = await pengoloMutation<{
        file: FileRecord;
        url: string;
        headers: Array<{ name: string; value: string }>;
      }>("beginPengoloUpload", {
        name: file.name,
        mime: file.type || "text/plain",
        size: file.size,
        checksum,
        projectId,
        temporarySession,
      });
      current();
      const response = await fetch(result.url, {
        method: "PUT",
        headers: Object.fromEntries(
          result.headers.map((header) => [header.name, header.value]),
        ),
        body: file,
        credentials: "omit",
      });
      if (!response.ok) throw new Error("Upload failed. Try again.");
      current();
      const ready = await pengoloMutation<FileRecord>("finishPengoloUpload", {
        id: result.file.id,
        temporarySession,
      });
      current();
      cache.clear();
      if (!temporarySession && selection === JSON.stringify([projectId, ""]))
        items.value.unshift(ready);
      return ready;
    } finally {
      uploading.value--;
    }
  }
  async function remove(id: string, temporarySession: string | null = null) {
    await pengoloMutation("deletePengoloFile", { id, temporarySession });
    cache.clear();
    items.value = items.value.filter((item) => item.id !== id);
  }
  async function download(id: string, temporarySession: string | null = null) {
    return pengoloMutation<{ url: string }>("pengoloDownload", {
      id,
      temporarySession,
    });
  }
  function clear() {
    identityEpoch++;
    loadEpoch++;
    selection = "";
    loading.value = false;
    cache.clear();
    items.value = [];
    matches.value = null;
    after.value = null;
  }
  return {
    items,
    loading,
    uploading,
    after,
    load,
    upload,
    remove,
    download,
    clear,
    matches,
    searchContents,
    index,
    refreshIndexing,
  };
});
