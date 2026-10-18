import {
  readCommandHistory,
  writeCommandHistory,
} from "~/utils/command-history.mjs";

export interface HistoryItem {
  label: string;
  description: string;
  type: string;
  isHistoryItem: true;
}

export function useCommandPaletteHistory() {
  const storageKey = "gg_command_history_v2";
  const { session } = useAccount();
  const commandHistory = ref<HistoryItem[]>([]);
  const scope = computed(() =>
    session.value
      ? { tenant: session.value.tenant, subject: session.value.subject }
      : null,
  );
  const item = (label: string): HistoryItem => ({
    label,
    description: "command_palette.history.fallback_desc",
    type: "Recent",
    isHistoryItem: true,
  });

  function restore() {
    commandHistory.value = [];
    try {
      commandHistory.value = readCommandHistory(
        localStorage.getItem(storageKey),
        scope.value,
      ).map(item);
    } catch {
      console.warn("Command history storage is unavailable");
    }
  }

  function save(queries: string[]) {
    const serialized = writeCommandHistory(queries, scope.value);
    commandHistory.value = readCommandHistory(serialized, scope.value).map(
      item,
    );
    try {
      if (serialized) localStorage.setItem(storageKey, serialized);
    } catch {
      console.warn("Command history could not be saved");
    }
  }

  function synchronize(event: StorageEvent) {
    if (event.key === storageKey || event.key === null) restore();
  }

  watch(scope, () => {
    if (import.meta.client) restore();
  });
  onMounted(() => {
    restore();
    window.addEventListener("storage", synchronize);
  });
  onUnmounted(() => window.removeEventListener("storage", synchronize));

  return {
    commandHistory,
    pushQueryToHistory: (query: string) =>
      save([query, ...commandHistory.value.map((entry) => entry.label)]),
    removeHistoryItem: (index: number) =>
      save(
        commandHistory.value
          .filter((_, position) => position !== index)
          .map((entry) => entry.label),
      ),
  };
}
