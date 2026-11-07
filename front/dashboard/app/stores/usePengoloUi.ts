import { pengoloMutation } from "~/utils/pengolo-api";
import { usePengoloRuns } from "~/stores/usePengoloRuns";
import { usePengoloChats } from "~/stores/usePengoloChats";
import { usePengoloAgents } from "~/stores/usePengoloAgents";
import { useLocalStorage } from "@vueuse/core";
import { defineStore } from "pinia";
import type { GraphContext, FileRecord } from "~/utils/pengolo-api";

export const usePengoloUi = defineStore("pengoloUi", () => {
  const sidebarOpen = ref(true);
  const drawerOpen = ref(false);
  const draftVersion = ref(0);
  const prompt = ref("");
  const executionMode = ref<"chat" | "research">("chat");
  const projectId = ref<string | null>(null);
  const graphContext = ref<GraphContext | null>(null);
  const files = ref<FileRecord[]>([]);
  const pendingFiles = ref(0);
  const temporary = ref(false);
  const withLibrary = ref(false);
  const temporarySession = ref<string | null>(null);
  const account = useState<{ subject: string; tenant: string } | null>(
    "account-session",
  );
  const preferenceKey = computed(
    () =>
      `pengolo:web:${JSON.stringify([account.value?.tenant ?? "", account.value?.subject ?? ""])}`,
  );
  const rememberedWebUsage = useLocalStorage(preferenceKey, false, {
    writeDefaults: false,
    flush: "sync",
  });
  const webEnabled = ref(rememberedWebUsage.value);
  watch(
    [preferenceKey, rememberedWebUsage],
    () => {
      webEnabled.value = rememberedWebUsage.value;
    },
    { flush: "sync" },
  );
  function rememberWebUsage(enabled: boolean) {
    rememberedWebUsage.value = enabled;
    webEnabled.value = enabled;
  }
  const webAvailable = ref(false);
  const error = ref("");
  function draft(project: string | null = null) {
    draftVersion.value++;
    usePengoloAgents().selected = [];
    prompt.value = "";
    executionMode.value = "chat";
    files.value = [];
    pendingFiles.value = 0;
    projectId.value = project;
    temporarySession.value = null;
    error.value = "";
    graphContext.value = null;
    webEnabled.value = rememberedWebUsage.value;
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
  /** Prevent a stored conversation from inheriting temporary-session scope. */
  async function prepareStoredChat() {
    await closeTemporary();
    temporary.value = false;
    withLibrary.value = false;
    files.value = [];
  }
  function clear() {
    draft();
    graphContext.value = null;
    temporary.value = false;
    withLibrary.value = false;
    webEnabled.value = false;
    drawerOpen.value = false;
  }
  return {
    draftVersion,
    sidebarOpen,
    drawerOpen,
    prompt,
    executionMode,
    projectId,
    graphContext,
    files,
    pendingFiles,
    temporary,
    withLibrary,
    temporarySession,
    webEnabled,
    rememberWebUsage,
    webAvailable,
    error,
    draft,
    closeTemporary,
    prepareStoredChat,
    clear,
  };
});
