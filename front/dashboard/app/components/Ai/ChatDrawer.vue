<script setup>
import { useAiChatStore } from "~/stores/useAiChat";
import {
  XMarkIcon,
  PaperAirplaneIcon,
  SparklesIcon,
  ArrowTopRightOnSquareIcon,
  ChatBubbleLeftRightIcon,
  DocumentIcon,
} from "@heroicons/vue/24/outline";

const router = useRouter();
const store = useAiChatStore();
const scrollContainer = ref(null);

const sampleSuggestions = [
  "Explain the current operational status",
  "What would happen if <event>?",
  "How can I use you?",
];

const scrollToBottom = async () => {
  await nextTick();
  if (scrollContainer.value) {
    scrollContainer.value.scrollTop = scrollContainer.value.scrollHeight;
  }
};

watch(() => store.chatMessages.length, scrollToBottom);

const handleStudioRedirect = () => {
  store.isChatOpen = false;
  router.push("/pengolo");
};

const sendChatMessage = async () => {
  const promptText = store.currentPrompt.trim();
  if ((!promptText && store.attachedFiles.length === 0) || store.isStreaming)
    return;

  const messageFiles = [...store.attachedFiles];

  store.chatMessages.push({
    id: crypto.randomUUID(),
    role: "user",
    text: promptText,
    files: messageFiles,
  });

  store.currentPrompt = "";
  store.attachedFiles = [];
  store.isStreaming = true;
  await scrollToBottom();

  const assistantMessageId = crypto.randomUUID();
  store.chatMessages.push({
    id: assistantMessageId,
    role: "assistant",
    text: "",
  });

  try {
    const response = await $fetch("/api/graphql", {
      method: "POST",
      body: {
        query: `mutation { ask(prompt: "${promptText}") }`,
      },
    });
    const targetIndex = store.chatMessages.findIndex(
      (m) => m.id === assistantMessageId,
    );
    if (targetIndex !== -1) {
      store.chatMessages[targetIndex].text =
        response?.data?.ask || `Response simulated.`;
    }
  } catch (error) {
    const targetIndex = store.chatMessages.findIndex(
      (m) => m.id === assistantMessageId,
    );
    if (targetIndex !== -1) {
      store.chatMessages[targetIndex].text =
        `[File(s): ${messageFiles.length}] Request processed by ${store.selectedModel.name}. Active Context: ${store.isPageContextActive ? "Yes" : "No"}.`;
    }
  } finally {
    store.isStreaming = false;
    await scrollToBottom();
  }
};
</script>

<template>
  <div
    v-if="store.isChatOpen"
    class="fixed bg-white border border-zinc-200/80 shadow-2xl transition-all duration-300 ease-in-out z-50 flex flex-col right-6 bottom-6 w-[440px] h-[660px] rounded-2xl overflow-hidden"
  >
    <div
      class="px-4 py-3.5 border-b border-zinc-100 flex items-center justify-between bg-zinc-50/50"
    >
      <div class="flex items-center space-x-2.5">
        <SparklesIcon class="w-4 h-4 text-zinc-900" />
        <span class="text-xs font-semibold text-zinc-900 tracking-tight">{{
          $t("chat_component.drawer.header_title")
        }}</span>
      </div>

      <div class="flex items-center space-x-1">
        <button
          @click="handleStudioRedirect"
          class="p-1.5 rounded-lg text-zinc-400 hover:text-zinc-900 hover:bg-zinc-100 transition-colors"
          :title="$t('chat_component.drawer.tooltips.split_workspace')"
        >
          <ArrowTopRightOnSquareIcon class="w-4 h-4" />
        </button>
        <button
          @click="store.isChatOpen = false"
          class="p-1.5 rounded-lg text-zinc-400 hover:text-zinc-900 hover:bg-zinc-100 transition-colors"
        >
          <XMarkIcon class="w-4 h-4" />
        </button>
      </div>
    </div>

    <div
      ref="scrollContainer"
      class="flex-1 overflow-y-auto p-4 space-y-4 bg-zinc-50/20"
    >
      <div
        v-if="store.chatMessages.length === 0"
        class="h-full flex flex-col justify-end pb-2"
      >
        <div class="flex flex-col items-center text-center mb-8">
          <div
            class="w-10 h-10 rounded-xl bg-zinc-50 flex items-center justify-center mb-3 border border-zinc-200"
          >
            <ChatBubbleLeftRightIcon class="w-5 h-5 text-zinc-700" />
          </div>
          <p class="text-xs text-zinc-500 max-w-xs leading-relaxed">
            <NuxtLink
              class="underline text-zinc-900 font-medium"
              to="https://aeluin-technologies.github.io/Galadril/studio/pengolo.html"
              external
              >{{ $t("chat_component.drawer.brand") }}</NuxtLink
            >
            {{ $t("chat_component.drawer.description") }}
          </p>
        </div>

        <div class="space-y-1.5 mb-2">
          <p
            class="text-[10px] font-bold text-zinc-400 uppercase tracking-wider px-1 mb-1"
          >
            {{ $t("chat_component.drawer.suggested_title") }}
          </p>
          <button
            v-for="(pill, idx) in sampleSuggestions"
            :key="idx"
            @click="store.currentPrompt = pill"
            class="w-full text-left flex items-center space-x-2.5 px-3 py-2.5 bg-white hover:bg-zinc-50 border border-zinc-200 text-xs text-zinc-800 rounded-xl transition-all duration-200 group"
          >
            <PaperAirplaneIcon
              class="w-3.5 h-3.5 text-zinc-400 group-hover:text-zinc-900 rotate-45 transform transition-transform"
            />
            <span class="truncate font-medium">{{ pill }}</span>
          </button>
        </div>
      </div>

      <div
        v-for="msg in store.chatMessages"
        :key="msg.id"
        :class="[
          'flex flex-col max-w-[88%]',
          msg.role === 'user' ? 'ml-auto items-end' : 'mr-auto items-start',
        ]"
      >
        <div
          v-if="msg.files && msg.files.length > 0"
          class="flex flex-wrap gap-1 mb-1 justify-end"
        >
          <div
            v-for="f in msg.files"
            :key="f.name"
            class="flex items-center space-x-1 bg-zinc-100 border border-zinc-200 text-[10px] text-zinc-600 px-2 py-0.5 rounded-md"
          >
            <DocumentIcon class="w-3 h-3 text-zinc-400" />
            <span class="truncate max-w-[120px]">{{ f.name }}</span>
          </div>
        </div>

        <div
          :class="[
            'px-3.5 py-2.5 rounded-2xl text-xs leading-relaxed border font-normal shadow-sm',
            msg.role === 'user'
              ? 'bg-zinc-900 border-zinc-900 text-white rounded-br-none'
              : 'bg-white border-zinc-200 text-zinc-900 rounded-bl-none',
          ]"
        >
          <span v-if="msg.text">{{ msg.text }}</span>
          <div v-else class="flex items-center space-x-1 py-1 px-1.5">
            <div class="w-1.5 h-1.5 bg-zinc-900 rounded-full animate-bounce" />
            <div
              class="w-1.5 h-1.5 bg-zinc-900 rounded-full animate-bounce [animation-delay:0.2s]"
            />
            <div
              class="w-1.5 h-1.5 bg-zinc-900 rounded-full animate-bounce [animation-delay:0.4s]"
            />
          </div>
        </div>
      </div>
    </div>

    <div class="p-4 border-t border-zinc-100 bg-white">
      <AiInputArea @submit="sendChatMessage" />
    </div>
  </div>
</template>
