import { defineStore } from "pinia";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  files?: Array<{ name: string; size: string; content?: string }>;
}

export interface ChatSession {
  id: string;
  title: string;
  messages: ChatMessage[];
  createdAt: number;
}

export interface AiModel {
  id: string;
  name: string;
}

export const useAiChatStore = defineStore("aiChat", () => {
  const isChatOpen = ref(false);
  const isStreaming = ref(false);
  const currentPrompt = ref("");
  const isPageContextActive = ref(true);
  const attachedFiles = ref<
    Array<{ name: string; size: string; content?: string }>
  >([]);
  const activeSessionId = ref<string | null>(null);
  const sessions = ref<ChatSession[]>([]);

  const availableModels: AiModel[] = [{ id: "gemma-4", name: "Gemma 4" }];

  const selectedModel = ref<AiModel>(availableModels[0]);

  onMounted(() => {
    const savedSessions = localStorage.getItem("pengolo_chat_sessions");
    if (savedSessions) {
      sessions.value = JSON.parse(savedSessions);
      if (sessions.value.length > 0) {
        activeSessionId.value = sessions.value[0].id;
      }
    }
  });

  watch(
    sessions,
    (newVal) => {
      if (import.meta.client) {
        localStorage.setItem("pengolo_chat_sessions", JSON.stringify(newVal));
      }
    },
    { deep: true },
  );

  const chatMessages = computed<ChatMessage[]>({
    get: () => {
      const current = sessions.value.find(
        (s) => s.id === activeSessionId.value,
      );
      return current ? current.messages : [];
    },
    set: (newMessages: ChatMessage[]) => {
      const current = sessions.value.find(
        (s) => s.id === activeSessionId.value,
      );
      if (current) current.messages = newMessages;
    },
  });

  const toggleChat = () => {
    isChatOpen.value = !isChatOpen.value;
  };

  const createNewSession = (title: string = "New Conversation"): string => {
    const newId = crypto.randomUUID();
    sessions.value.unshift({
      id: newId,
      title: title.trim(),
      messages: [],
      createdAt: Date.now(),
    });
    activeSessionId.value = newId;
    return newId;
  };

  const addFiles = (files: FileList) => {
    for (const file of Array.from(files)) {
      if (attachedFiles.value.length < 3) {
        attachedFiles.value.push({
          name: file.name,
          size: (file.size / 1024).toFixed(1) + " KB",
          content: `Contents of ${file.name} context read.`,
        });
      } else {
        break;
      }
    }
  };

  const removeFile = (index: number) => {
    attachedFiles.value.splice(index, 1);
  };

  const deleteSession = (id: string) => {
    const idx = sessions.value.findIndex((s) => s.id === id);
    if (idx !== -1) {
      sessions.value.splice(idx, 1);
      if (activeSessionId.value === id) {
        activeSessionId.value = sessions.value[0]?.id || null;
      }
    }
  };

  return {
    isChatOpen,
    isStreaming,
    currentPrompt,
    isPageContextActive,
    attachedFiles,
    sessions,
    activeSessionId,
    chatMessages,
    availableModels,
    selectedModel,
    toggleChat,
    createNewSession,
    addFiles,
    removeFile,
    deleteSession,
  };
});
