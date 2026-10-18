import { defineStore } from "pinia";
import { createRequestCache } from "~/utils/pengolo";
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
  async function load(
    projectId: string | null = null,
    search = "",
    more = false,
  ) {
    const epoch = ++loadEpoch;
    selection = JSON.stringify([projectId, search]);
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
    await pengoloRequest<boolean>(pengoloOperation("IndexDocument"), { id });
    cache.clear();
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
  };
});
