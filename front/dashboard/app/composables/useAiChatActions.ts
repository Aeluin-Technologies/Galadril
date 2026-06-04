import { useAiChatStore } from "@/stores/useAiChat";

export function useAiChatActions() {
  const store = useAiChatStore();

  const scrollToBottom = async (scrollContainerRef: any) => {
    await nextTick();
    if (scrollContainerRef?.value) {
      scrollContainerRef.value.scrollTop =
        scrollContainerRef.value.scrollHeight;
    }
  };

  const handleIncomingFiles = (files: FileList | null): boolean => {
    if (!files || files.length === 0) return true;
    if (store.attachedFiles.length + files.length > 3) return false;
    store.addFiles(files);
    return true;
  };

  const sendChatMessage = async (scrollContainerRef?: any) => {
    const promptText = store.currentPrompt.trim();
    if ((!promptText && store.attachedFiles.length === 0) || store.isStreaming)
      return;

    if (!store.activeSessionId) {
      store.createNewSession(promptText || "File Upload Session");
    }

    const messageFiles = [...store.attachedFiles];

    store.chatMessages.push({
      id: crypto.randomUUID(),
      role: "user",
      text: promptText,
      files: messageFiles,
    });

    const sessionToUpdate = store.sessions.find(
      (s: { id: string }) => s.id === store.activeSessionId,
    );
    if (
      sessionToUpdate &&
      sessionToUpdate.title === "New Conversation" &&
      promptText
    ) {
      sessionToUpdate.title = promptText;
    }

    store.currentPrompt = "";
    store.attachedFiles = [];
    store.isStreaming = true;
    if (scrollContainerRef) await scrollToBottom(scrollContainerRef);

    const assistantMessageId = crypto.randomUUID();
    store.chatMessages.push({
      id: assistantMessageId,
      role: "assistant",
      text: "",
    });

    try {
      const response: any = await $fetch("/api/graphql", {
        method: "POST",
        body: { query: `mutation { ask(prompt: "${promptText}") }` },
      });

      const targetIndex = store.chatMessages.findIndex(
        (m: { id: string }) => m.id === assistantMessageId,
      );
      if (targetIndex !== -1 && store.chatMessages[targetIndex]) {
        store.chatMessages[targetIndex].text =
          response?.data?.ask ||
          `Response generated via ${store.selectedModel.name}.`;
      }
    } catch (error) {
      const targetIndex = store.chatMessages.findIndex(
        (m: { id: string }) => m.id === assistantMessageId,
      );
      if (targetIndex !== -1 && store.chatMessages[targetIndex]) {
        store.chatMessages[targetIndex].text =
          "Pengolo is not usable at the moment.";
      }
    } finally {
      store.isStreaming = false;
      if (scrollContainerRef) await scrollToBottom(scrollContainerRef);
    }
  };

  return {
    sendChatMessage,
    scrollToBottom,
    handleIncomingFiles,
  };
}
