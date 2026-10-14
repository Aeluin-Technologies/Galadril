import { pengoloMutation } from "~/utils/pengolo-api";
import { usePengoloRuns } from "~/stores/usePengoloRuns";
import { usePengoloChats } from "~/stores/usePengoloChats";
import { defineStore } from "pinia";
import type { GraphContext, FileRecord } from "~/utils/pengolo-api";

export const usePengoloUi = defineStore("pengoloUi", () => {
  const sidebarOpen = ref(true);
  const drawerOpen = ref(false);
  const draftVersion = ref(0);
  const prompt = ref("");
  const projectId = ref<string | null>(null);
  const graphContext = ref<GraphContext | null>(null);
  const files = ref<FileRecord[]>([]);
  const temporary = ref(false);
  const withLibrary = ref(false);
  const temporarySession = ref<string | null>(null);
  const webEnabled = ref(false);
  const webAvailable = ref(false);
  const webQuery = ref("");
  const error = ref("");
  function draft(project: string | null = null) {
    draftVersion.value++;
    prompt.value = "";
    files.value = [];
    projectId.value = project;
    temporarySession.value = null;
    error.value = "";
    graphContext.value = null;
    webEnabled.value = false;
    webQuery.value = "";
  }
  async function closeTemporary() {
    if (!temporary.value) return;
    const session = temporarySession.value ?? usePengoloChats().active?.id;
    if (session) {
      usePengoloRuns().forgetTemporary(session);
      try {
        await pengoloMutation("closePengoloTemporary", { id: session });
      } catch {
        error.value =
          "The temporary session is closed here. Its files will expire automatically.";
      }
    }
    usePengoloChats().active = null;
    prompt.value = "";
    files.value = [];
    temporarySession.value = null;
  }
  function clear() {
    draft();
    graphContext.value = null;
    temporary.value = false;
    withLibrary.value = false;
    webEnabled.value = false;
    webQuery.value = "";
    drawerOpen.value = false;
  }
  return {
    draftVersion,
    sidebarOpen,
    drawerOpen,
    prompt,
    projectId,
    graphContext,
    files,
    temporary,
    withLibrary,
    temporarySession,
    webEnabled,
    webAvailable,
    webQuery,
    error,
    draft,
    closeTemporary,
    clear,
  };
});
