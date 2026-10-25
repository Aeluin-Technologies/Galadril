import { usePengoloLibrary } from "~/stores/usePengoloLibrary";
import { usePengoloUi } from "~/stores/usePengoloUi";
import type { FileRecord } from "~/utils/pengolo-api";
import { downloadDocuments } from "~/utils/document-download";

/** Selection and pending operations belong to the current identity and library scope. */
export function useDocumentSelection(scope: () => string) {
  const library = usePengoloLibrary(),
    ui = usePengoloUi();
  const { t } = useI18n();
  const selected = ref<string[]>([]),
    deleting = shallowRef<FileRecord[]>([]);
  const busy = ref(false);
  let epoch = 0;
  let controller: AbortController | undefined;
  const files = computed(() =>
    library.items.filter((file) => selected.value.includes(file.id)),
  );
  const all = computed(
    () =>
      library.items.length > 0 && files.value.length === library.items.length,
  );
  const ready = computed(
    () =>
      files.value.length > 0 &&
      files.value.every((file) => file.data.status === "ready"),
  );
  function toggle(id: string, checked: boolean) {
    selected.value = checked
      ? [...new Set([...selected.value, id])]
      : selected.value.filter((value) => value !== id);
  }
  function toggleAll(checked: boolean) {
    selected.value = checked ? library.items.map((file) => file.id) : [];
  }
  function clear() {
    epoch++;
    controller?.abort();
    controller = undefined;
    selected.value = [];
    deleting.value = [];
    busy.value = false;
  }
  watch(scope, clear);
  watch(
    () => library.items,
    () => {
      const ids = new Set(library.items.map((file) => file.id));
      selected.value = selected.value.filter((id) => ids.has(id));
    },
  );
  onDeactivated(clear);
  onBeforeUnmount(clear);
  async function remove() {
    if (busy.value) return;
    const own = epoch,
      pending = [...deleting.value];
    busy.value = true;
    const failed: string[] = [];
    try {
      for (const file of pending) {
        if (own !== epoch) return;
        try {
          await library.remove(file.id);
        } catch {
          failed.push(file.id);
        }
      }
      if (own !== epoch) return;
      deleting.value = [];
      selected.value = failed;
      if (failed.length) ui.error = t("documents.partialFailure");
    } finally {
      if (own === epoch) busy.value = false;
    }
  }
  async function download() {
    if (busy.value) return;
    const own = epoch;
    controller = new AbortController();
    const signal = controller.signal;
    busy.value = true;
    try {
      const blob = await downloadDocuments(
        [...files.value],
        (id) => library.download(id),
        signal,
      );
      if (own !== epoch) return;
      const url = URL.createObjectURL(blob),
        link = document.createElement("a");
      link.href = url;
      link.download = "Galadril-documents.zip";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    } catch {
      if (own === epoch) ui.error = t("documents.downloadError");
    } finally {
      if (own === epoch) {
        busy.value = false;
        controller = undefined;
      }
    }
  }
  return {
    selected,
    deleting,
    busy,
    files,
    all,
    ready,
    toggle,
    toggleAll,
    clear,
    remove,
    download,
  };
}
