<script setup>
import { useAiChatStore } from "~/stores/useAiChat";
import { useAiChatActions } from "~/composables/useAiChatActions";
import {
  PaperAirplaneIcon,
  BookOpenIcon,
  PlusIcon,
  ChevronDownIcon,
  CheckIcon,
  XMarkIcon,
  DocumentIcon,
  ExclamationCircleIcon,
} from "@heroicons/vue/24/outline";

const emit = defineEmits(["submit"]);
const store = useAiChatStore();
const { handleIncomingFiles } = useAiChatActions();

const isModelMenuOpen = ref(false);
const fileInputRef = ref(null);
const dropdownRef = ref(null);
const showFileAlert = ref(false);

const triggerFileInput = () => {
  if (store.attachedFiles.length >= 3) {
    showFileAlert.value = true;
    setTimeout(() => {
      showFileAlert.value = false;
    }, 4000);
    return;
  }
  fileInputRef.value?.click();
};

const handleFileUpload = (event) => {
  const target = event.target;
  if (target && target.files) {
    const success = handleIncomingFiles(target.files);
    if (!success) {
      showFileAlert.value = true;
      setTimeout(() => {
        showFileAlert.value = false;
      }, 4000);
    }
  }
  event.target.value = "";
};

const selectModel = (model) => {
  store.selectedModel = model;
  isModelMenuOpen.value = false;
};

const handleKeyDown = (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    emit("submit");
  }
};

const handleClickOutside = (event) => {
  if (dropdownRef.value && !dropdownRef.value.contains(event.target)) {
    isModelMenuOpen.value = false;
  }
};

onMounted(() => {
  document.addEventListener("click", handleClickOutside);
});
onUnmounted(() => {
  document.removeEventListener("click", handleClickOutside);
});
</script>

<template>
  <div class="w-full relative">
    <Transition
      enter-active-class="transition duration-200 ease-out"
      enter-from-class="transform translate-y-2 opacity-0"
      enter-to-class="transform translate-y-0 opacity-100"
      leave-active-class="transition duration-150 ease-in"
      leave-from-class="transform translate-y-0 opacity-100"
      leave-to-class="transform translate-y-2 opacity-0"
    >
      <div
        v-if="showFileAlert"
        class="absolute -top-12 left-0 right-0 z-50 flex justify-center"
      >
        <div
          class="bg-stone-900 border border-zinc-800 text-white text-xs px-3 py-2 rounded-xl flex items-center space-x-2 shadow-xl backdrop-blur-md"
        >
          <ExclamationCircleIcon class="w-4 h-4 text-amber-500" />
          <span>{{ $t("chat_component.input.file_limit_error") }}</span>
        </div>
      </div>
    </Transition>

    <div
      v-if="store.attachedFiles.length > 0"
      class="flex flex-wrap gap-1.5 mb-3 px-1"
    >
      <div
        v-for="(file, i) in store.attachedFiles"
        :key="i"
        class="flex items-center space-x-2 bg-stone-50 border border-zinc-200 text-[11px] text-zinc-800 px-2.5 py-1 rounded-lg transition-all"
      >
        <DocumentIcon class="w-3.5 h-3.5 text-zinc-400" />
        <span class="truncate max-w-[140px] font-medium text-zinc-700">{{
          file.name
        }}</span>
        <span class="text-[10px] text-zinc-400 font-mono"
          >({{ file.size }})</span
        >
        <button
          @click="store.removeFile(i)"
          class="text-zinc-400 hover:text-zinc-900 ml-1"
        >
          <XMarkIcon class="w-3 h-3" />
        </button>
      </div>
    </div>

    <div
      class="border border-zinc-200 focus-within:border-zinc-900 focus-within:ring-1 focus-within:ring-zinc-900 rounded-xl p-3 transition-all bg-white flex flex-col min-h-[120px] justify-between"
    >
      <textarea
        v-model="store.currentPrompt"
        rows="2"
        :placeholder="$t('chat_component.input.placeholder')"
        @keydown="handleKeyDown"
        class="w-full resize-none bg-transparent border-none text-xs text-zinc-900 focus:ring-0 outline-none p-1 placeholder:text-zinc-400 leading-relaxed"
      />

      <div
        class="flex items-center justify-between pt-2.5 border-t border-zinc-100 relative"
      >
        <div class="flex items-center space-x-1.5">
          <button
            type="button"
            @click="store.isPageContextActive = !store.isPageContextActive"
            :class="[
              'p-2 rounded-lg border transition-all duration-200',
              store.isPageContextActive
                ? 'bg-amber-500/10 border-amber-500/30 text-amber-700 font-medium'
                : 'bg-white border-zinc-200 text-zinc-400 hover:text-zinc-900 hover:bg-stone-50',
            ]"
            :title="$t('chat_component.input.tooltips.context')"
          >
            <BookOpenIcon class="w-4 h-4" />
          </button>

          <input
            type="file"
            ref="fileInputRef"
            multiple
            class="hidden"
            @change="handleFileUpload"
          />
          <button
            type="button"
            @click="triggerFileInput"
            class="p-2 rounded-lg bg-white border border-zinc-200 text-zinc-400 hover:text-zinc-900 hover:bg-stone-50 transition-all"
            :title="$t('chat_component.input.tooltips.attach')"
          >
            <PlusIcon class="w-4 h-4" />
          </button>

          <div class="relative" ref="dropdownRef">
            <button
              type="button"
              @click.stop="isModelMenuOpen = !isModelMenuOpen"
              class="flex items-center space-x-1.5 px-3 py-2 rounded-lg bg-white border border-zinc-200 text-xs text-zinc-800 hover:bg-stone-50 font-medium"
            >
              <span>{{ store.selectedModel.name }}</span>
              <ChevronDownIcon class="w-3 h-3 text-zinc-400" />
            </button>

            <div
              v-if="isModelMenuOpen"
              class="absolute bottom-full left-0 mb-2 w-56 bg-white border border-zinc-200 rounded-xl shadow-xl z-50 py-1 text-xs"
            >
              <div
                class="px-3 py-1.5 text-[10px] font-bold text-zinc-400 uppercase bg-stone-50"
              >
                {{ $t("chat_component.input.model_categories.fast") }}
              </div>
              <button
                type="button"
                v-for="m in store.availableModels"
                :key="m.id"
                @click="selectModel(m)"
                class="w-full px-3 py-2 flex items-center justify-between hover:bg-stone-50 text-left font-medium"
              >
                <span>{{ m.name }}</span>
                <CheckIcon
                  v-if="store.selectedModel.id === m.id"
                  class="w-3.5 h-3.5 text-zinc-900"
                />
              </button>
            </div>
          </div>
        </div>

        <button
          type="button"
          @click="emit('submit')"
          :disabled="
            (!store.currentPrompt.trim() && store.attachedFiles.length === 0) ||
            store.isStreaming
          "
          class="p-2 rounded-lg bg-stone-900 text-white hover:bg-stone-800 disabled:bg-stone-100 disabled:text-zinc-400 transition-all shrink-0"
        >
          <PaperAirplaneIcon class="w-4 h-4 transform rotate-90" />
        </button>
      </div>
    </div>
  </div>
</template>
