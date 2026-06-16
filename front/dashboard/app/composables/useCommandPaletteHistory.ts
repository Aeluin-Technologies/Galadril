export interface HistoryItem {
  label: string;
  description: string;
  type: string;
  isHistoryItem: true;
}

export function useCommandPaletteHistory(maxItems = 5) {
  const commandHistory = ref<HistoryItem[]>([]);

  onMounted(() => {
    const saved = localStorage.getItem("gg_command_history");
    if (saved) {
      try {
        commandHistory.value = JSON.parse(saved);
      } catch (e) {
        commandHistory.value = [];
      }
    }
  });

  const pushQueryToHistory = (queryText: string): void => {
    const cleanText = queryText.trim();
    if (!cleanText) return;

    let list = commandHistory.value.filter((h) => h.label !== cleanText);

    list.unshift({
      label: cleanText,
      description: "command_palette.history.fallback_desc",
      type: "Recent",
      isHistoryItem: true,
    });

    if (list.length > maxItems) {
      list = list.slice(0, maxItems);
    }

    commandHistory.value = list;
    localStorage.setItem("gg_command_history", JSON.stringify(list));
  };

  const removeHistoryItem = (index: number): void => {
    const updated = [...commandHistory.value];
    updated.splice(index, 1);
    commandHistory.value = updated;
    localStorage.setItem("gg_command_history", JSON.stringify(updated));
  };

  return {
    commandHistory,
    pushQueryToHistory,
    removeHistoryItem,
  };
}
