import { defineStore } from "pinia";
import { usePengoloUi } from "~/stores/usePengoloUi";

export const useAiChatStore = defineStore("aiChat", () => {
  const ui = usePengoloUi();
  const isChatOpen = computed({
    get: () => ui.drawerOpen,
    set: (open: boolean) => {
      ui.drawerOpen = open;
    },
  });
  function toggleChat(): void {
    const route = useRoute();
    if (!ui.drawerOpen) {
      const match = /^\/explorer\/(entity|event|relation)\/([^/]+)$/.exec(
        route.path,
      );
      const kind = match?.[1];
      const id = match?.[2];
      if (id && (kind === "entity" || kind === "event" || kind === "relation"))
        ui.graphContext = { kind, id };
    }
    ui.drawerOpen = !ui.drawerOpen;
  }
  return { isChatOpen, toggleChat };
});
