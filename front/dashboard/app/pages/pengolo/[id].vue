<script setup>
import { useAiChatStore } from "~/stores/useAiChat";
import { useAiChatActions } from "~/composables/useAiChatActions";
import {
  PlusIcon,
  SparklesIcon,
  DocumentIcon,
  TrashIcon,
  Square2StackIcon,
  XMarkIcon,
} from "@heroicons/vue/24/outline";

const route = useRoute();
const router = useRouter();
const store = useAiChatStore();
const { sendChatMessage, scrollToBottom } = useAiChatActions();

const scrollContainer = ref(null);
const previewFile = ref(null);

const syncRouteWithStore = () => {
  const sessionId = route.params.id;
  if (sessionId && store.sessions) {
    const sessionExists = store.sessions.some((s) => s.id === sessionId);
    if (sessionExists) {
      store.activeSessionId = sessionId;
    } else {
      router.replace("/pengolo");
    }
  }
};

onMounted(() => {
  syncRouteWithStore();
  scrollToBottom(scrollContainer);
});

watch(
  () => route.params.id,
  () => {
    syncRouteWithStore();
    scrollToBottom(scrollContainer);
  },
);

watch(
  () => store.chatMessages.length,
  () => {
    scrollToBottom(scrollContainer);
  },
  { deep: true },
);

const selectSession = (id) => {
  router.push(`/pengolo/${id}`);
};

const handleStudioSubmit = () => {
  sendChatMessage(scrollContainer);
};

const createNewChat = () => {
  const id = store.createNewSession("New Conversation");
  router.push(`/pengolo/${id}`);
};

const handleDeleteSession = (id, event) => {
  event.stopPropagation();
  store.deleteSession(id);
  if (store.activeSessionId === null) {
    router.push("/pengolo");
  } else if (store.activeSessionId !== route.params.id) {
    router.push(`/pengolo/${store.activeSessionId}`);
  }
};

const handleUpdateMessage = async ({ id, text }) => {
  const targetIndex = store.chatMessages.findIndex((m) => m.id === id);
  if (targetIndex !== -1) {
    store.chatMessages[targetIndex].text = text;
  }

  const assistantIndex = targetIndex + 1;
  if (
    assistantIndex < store.chatMessages.length &&
    store.chatMessages[assistantIndex].role === "assistant"
  ) {
    store.chatMessages[assistantIndex].text = "";
    store.isStreaming = true;

    try {
      const response = await $fetch("/api/graphql", {
        method: "POST",
        body: {
          query: `mutation { ask(prompt: "${store.chatMessages[targetIndex].text}") }`,
        },
      });
      store.chatMessages[assistantIndex].text =
        response?.data?.ask || "Response simulated.";
    } catch (error) {
      store.chatMessages[assistantIndex].text =
        `Request processed by ${store.selectedModel?.name || "Gemma 4"}. Active Context: ${store.isPageContextActive ? "Yes" : "No"}.`;
    } finally {
      store.isStreaming = false;
      scrollToBottom(scrollContainer);
    }
  }
};
</script>

<template>
  <div
    class="flex h-screen w-screen bg-stone-50 text-zinc-900 font-sans overflow-hidden"
  >
    <aside
      class="w-72 border-r border-zinc-200 bg-stone-50 flex flex-col justify-between shrink-0"
    >
      <div class="p-4 flex flex-col space-y-4 overflow-hidden flex-1">
        <UtilsButton
          @click="createNewChat"
          variant="primary"
          class="w-full !py-2.5 !px-3 rounded-xl !text-xs font-medium flex items-center justify-center space-x-2 shadow-sm"
        >
          <PlusIcon class="w-4 h-4" />
          <span>{{ $t("chat_component.studio.new_chat") }}</span>
        </UtilsButton>

        <div class="space-y-1.5 pt-2 flex-1 flex flex-col overflow-hidden">
          <p
            class="text-[10px] font-bold text-zinc-400 uppercase tracking-wider px-1 mb-1"
          >
            {{ $t("chat_component.studio.recent_sessions") }}
          </p>

          <div
            class="flex-1 overflow-y-auto space-y-0.5 pr-1"
            v-if="store.sessions"
          >
            <div
              v-for="session in store.sessions"
              :key="session.id"
              @click="selectSession(session.id)"
              :class="[
                'w-full flex items-center justify-between px-3 py-2.5 rounded-xl text-xs font-medium cursor-pointer transition-all group border border-transparent',
                session.id === store.activeSessionId
                  ? 'bg-stone-100 border-zinc-200/60 text-zinc-900 shadow-sm'
                  : 'text-zinc-500 hover:bg-stone-50 hover:text-zinc-800',
              ]"
            >
              <div class="flex items-center space-x-2.5 truncate flex-1">
                <Square2StackIcon class="w-3.5 h-3.5 shrink-0 opacity-60" />
                <span class="truncate pr-2">{{
                  session.title || "Untitled Conversation"
                }}</span>
              </div>
              <button
                @click="handleDeleteSession(session.id, $event)"
                class="opacity-0 group-hover:opacity-100 text-zinc-400 hover:text-zinc-900 p-1 rounded-lg transition-all shrink-0"
              >
                <TrashIcon class="w-3.5 h-3.5" />
              </button>
            </div>
          </div>
        </div>
      </div>

      <div
        class="p-4 border-t border-zinc-100 flex items-center space-x-2 bg-stone-50 shrink-0"
      >
        <SparklesIcon class="w-4 h-4 text-zinc-900 animate-pulse" />
        <span class="text-xs font-semibold text-zinc-800">
          {{ $t("chat_component.studio.footer_brand") }}
        </span>
      </div>
    </aside>

    <main class="flex-1 flex flex-col h-full bg-white relative overflow-hidden">
      <div
        class="h-14 border-b border-zinc-100 px-8 flex items-center justify-between shrink-0 bg-white/80 backdrop-blur z-10"
      >
        <div class="flex items-center space-x-3">
          <h1 class="text-xs font-semibold text-zinc-800 truncate max-w-md">
            {{
              store.sessions?.find((s) => s.id === store.activeSessionId)
                ?.title || $t("chat_component.studio.fallback_title")
            }}
          </h1>
          <span
            class="px-2 py-0.5 rounded-full text-[10px] font-medium bg-stone-100 text-zinc-500 border border-zinc-200"
          >
            {{ store.selectedModel.name }}
          </span>
        </div>
      </div>

      <div
        ref="scrollContainer"
        class="flex-1 overflow-y-auto px-8 pt-6 pb-40 bg-stone-50/40 relative"
      >
        <div
          v-if="store.chatMessages.length === 0"
          class="absolute inset-0 flex flex-col items-center justify-center p-8 text-center bg-white z-20"
        >
          <div class="max-w-xl w-full flex flex-col items-center">
            <div
              class="w-12 h-12 bg-stone-50 rounded-2xl border border-zinc-200 shadow-sm flex items-center justify-center mb-4"
            >
              <SparklesIcon class="w-5 h-5 text-zinc-900 animate-pulse" />
            </div>
            <h3 class="text-sm font-semibold text-zinc-900">
              {{ $t("chat_component.studio.welcome_title") }}
            </h3>
            <p
              class="text-xs text-zinc-400 mt-1.5 max-w-sm leading-relaxed mb-8"
            >
              {{ $t("chat_component.studio.welcome_subtitle") }}
            </p>
            <div
              class="w-full max-w-2xl bg-white rounded-xl shadow-md border border-zinc-200/80 p-1"
            >
              <AiInputArea @submit="handleStudioSubmit" />
            </div>
          </div>
        </div>

        <div v-else class="space-y-6 max-w-5xl mx-auto w-full flex flex-col">
          <AiChatMessage
            v-for="msg in store.chatMessages"
            :key="msg.id"
            :msg="msg"
            :isStreaming="store.isStreaming"
            @update:text="handleUpdateMessage"
            @preview-file="previewFile = $event"
          />

          <div class="h-32 w-full shrink-0 pointer-events-none" />
        </div>
      </div>

      <div
        v-if="store.chatMessages.length > 0"
        class="fixed bottom-6 right-0 w-[calc(100vw-18rem)] px-8 pointer-events-none z-30"
      >
        <div
          class="max-w-3xl mx-auto w-full bg-white rounded-xl shadow-xl border border-zinc-200/80 p-1 pointer-events-auto"
        >
          <AiInputArea @submit="handleStudioSubmit" />
        </div>
      </div>

      <Transition
        enter-active-class="transform transition ease-in-out duration-300"
        enter-from-class="translate-x-full"
        enter-to-class="translate-x-0"
        leave-active-class="transform transition ease-in-out duration-200"
        leave-from-class="translate-x-0"
        leave-to-class="translate-x-full"
      >
        <div
          v-if="previewFile"
          class="absolute inset-y-0 right-0 w-96 bg-white border-l border-zinc-200 shadow-2xl z-50 flex flex-col"
        >
          <div
            class="px-4 h-14 border-b border-zinc-100 flex items-center justify-between bg-stone-50 shrink-0"
          >
            <div class="flex items-center space-x-2 truncate">
              <DocumentIcon class="w-4 h-4 text-zinc-500 shrink-0" />
              <span class="text-xs font-semibold text-zinc-800 truncate">{{
                previewFile.name
              }}</span>
            </div>
            <button
              @click="previewFile = null"
              class="p-1.5 hover:bg-stone-200 text-zinc-400 hover:text-zinc-900 rounded-lg"
            >
              <XMarkIcon class="w-4 h-4" />
            </button>
          </div>
          <div
            class="flex-1 p-4 overflow-y-auto font-mono text-[11px] text-zinc-600 bg-stone-50/50 whitespace-pre-wrap select-text leading-relaxed"
          >
            {{
              previewFile.content ||
              $t("chat_component.studio.file_preview_empty")
            }}
          </div>
        </div>
      </Transition>
    </main>
  </div>
</template>
