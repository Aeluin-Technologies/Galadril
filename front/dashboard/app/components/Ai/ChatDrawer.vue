<script setup>
import { useAiChatStore } from "~/stores/useAiChat";
import {
  XMarkIcon,
  SparklesIcon,
  ArrowTopRightOnSquareIcon,
  ChatBubbleLeftRightIcon,
} from "@heroicons/vue/24/outline";
import { PaperAirplaneIcon } from "@heroicons/vue/24/solid";

const { t } = useI18n();
const router = useRouter();
const store = useAiChatStore();
const scrollContainer = ref(null);

const sampleSuggestions = [
  t("chat_component.suggestions.operational_status"),
  t("chat_component.suggestions.what_if_event"),
  t("chat_component.suggestions.how_to_use"),
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
        response?.data?.ask || t("chat_component.responses.simulated");
    } catch (error) {
      store.chatMessages[assistantIndex].text = t(
        "chat_component.responses.error_context",
        {
          model: store.selectedModel?.name || "Gemma 4",
          context: store.isPageContextActive
            ? t("chat_component.responses.yes")
            : t("chat_component.responses.no"),
        },
      );
    } finally {
      store.isStreaming = false;
      await scrollToBottom();
    }
  }
};

const sendChatMessage = async () => {
  const promptText = store.currentPrompt ? store.currentPrompt.trim() : "";
  if (!promptText && store.attachedFiles?.length === 0) return;
  if (store.isStreaming) return;

  const messageFiles = store.attachedFiles ? [...store.attachedFiles] : [];

  store.chatMessages.push({
    id: crypto.randomUUID(),
    role: "user",
    text: promptText,
    files: messageFiles,
  });

  store.currentPrompt = "";
  if (store.attachedFiles) store.attachedFiles = [];
  store.isStreaming = true;
  await scrollToBottom();

  const assistantMessageId = crypto.randomUUID();
  store.chatMessages.push({
    id: assistantMessageId,
    role: "assistant",
    text: "",
  });
  await scrollToBottom();

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
        response?.data?.ask || t("chat_component.responses.simulated");
    }
  } catch (error) {
    const targetIndex = store.chatMessages.findIndex(
      (m) => m.id === assistantMessageId,
    );
    if (targetIndex !== -1) {
      store.chatMessages[targetIndex].text = t(
        "chat_component.responses.error_context",
        {
          model: store.selectedModel?.name || "Gemma 4",
          context: store.isPageContextActive
            ? t("chat_component.responses.yes")
            : t("chat_component.responses.no"),
        },
      );
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
      class="px-4 py-3.5 border-b border-zinc-100 flex items-center justify-between bg-stone-50/50"
    >
      <div class="flex items-center space-x-2.5">
        <SparklesIcon class="w-4 h-4 text-amber-500" />
        <span class="text-xs font-semibold text-zinc-900 tracking-tight">
          {{ $t("chat_component.drawer.header_title") }}
        </span>
      </div>

      <div class="flex items-center space-x-1">
        <button
          @click="handleStudioRedirect"
          class="p-1.5 rounded-lg text-zinc-400 hover:text-zinc-900 hover:bg-stone-100 transition-colors"
          :title="$t('chat_component.drawer.tooltips.split_workspace')"
        >
          <ArrowTopRightOnSquareIcon class="w-4 h-4" />
        </button>
        <button
          @click="store.isChatOpen = false"
          class="p-1.5 rounded-lg text-zinc-400 hover:text-zinc-900 hover:bg-stone-100 transition-colors"
        >
          <XMarkIcon class="w-4 h-4" />
        </button>
      </div>
    </div>

    <div
      ref="scrollContainer"
      class="flex-1 overflow-y-auto p-4 space-y-5 bg-white"
    >
      <div
        v-if="store.chatMessages.length === 0"
        class="h-full flex flex-col justify-end pb-2"
      >
        <div class="flex flex-col items-center text-center mb-8">
          <div
            class="w-10 h-10 rounded-xl bg-amber-50 flex items-center justify-center mb-3 border border-amber-100"
          >
            <ChatBubbleLeftRightIcon class="w-5 h-5 text-amber-600" />
          </div>
          <p class="text-xs text-zinc-500 max-w-xs leading-relaxed">
            <NuxtLink
              class="underline text-zinc-900 font-medium hover:text-amber-600"
              to="https://aeluin-technologies.github.io/Galadril/studio/pengolo.html"
              external
            >
              {{ $t("chat_component.drawer.brand") }}
            </NuxtLink>
            {{ $t("chat_component.drawer.description") }}
          </p>
        </div>

        <div class="space-y-1.5 mb-2">
          <p
            class="text-[10px] font-bold text-zinc-400 uppercase tracking-wider px-1 mb-1"
          >
            {{ $t("chat_component.drawer.suggested_title") }}
          </p>
          <UtilsButton
            v-for="(pill, idx) in sampleSuggestions"
            :key="idx"
            variant="secondary"
            @click="store.currentPrompt = pill"
            class="w-full !justify-start flex items-center space-x-3 px-4 py-2.5 rounded-xl border border-zinc-200"
          >
            <PaperAirplaneIcon
              class="w-3.5 h-3.5 text-amber-500 flex-shrink-0"
            />
            <span
              class="truncate font-medium text-zinc-700 text-xs text-left"
              >{{ pill }}</span
            >
          </UtilsButton>
        </div>
      </div>

      <div v-else class="space-y-4">
        <AiChatMessage
          v-for="msg in store.chatMessages"
          :key="msg.id"
          :msg="msg"
          :isStreaming="store.isStreaming"
          @update:text="handleUpdateMessage"
        />
      </div>
    </div>

    <div class="p-4 border-t border-zinc-100 bg-white">
      <AiInputArea v-model="store.currentPrompt" @submit="sendChatMessage" />
    </div>
  </div>
</template>
