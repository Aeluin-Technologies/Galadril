<script setup>
import { XMarkIcon } from "@heroicons/vue/24/outline";

defineProps({
  isOpen: Boolean,
  title: String,
});
const emit = defineEmits(["close"]);
</script>

<template>
  <Teleport to="body">
    <Transition
      enter-active-class="transition-opacity duration-200"
      enter-from-class="opacity-0"
      enter-to-class="opacity-100"
      leave-active-class="transition-opacity duration-150"
      leave-from-class="opacity-100"
      leave-to-class="opacity-0"
    >
      <div
        v-if="isOpen"
        class="fixed inset-0 bg-slate-900/40 backdrop-blur-sm z-[200] flex justify-center items-center p-4"
        @click.self="emit('close')"
      >
        <div
          class="bg-white rounded-xl shadow-2xl w-full max-w-lg flex flex-col overflow-hidden"
        >
          <div
            class="px-5 py-4 border-b border-zinc-100 flex justify-between items-center bg-stone-50"
          >
            <h3 class="text-lg font-semibold text-zinc-800">{{ title }}</h3>
            <button
              @click="emit('close')"
              class="text-zinc-400 hover:text-zinc-600 transition-colors"
            >
              <XMarkIcon class="w-5 h-5" />
            </button>
          </div>
          <div class="p-5 overflow-y-auto max-h-[70vh]">
            <slot />
          </div>
          <div
            class="px-5 py-4 border-t border-zinc-100 bg-stone-50 flex justify-end gap-3"
          >
            <slot name="footer">
              <button
                @click="emit('close')"
                class="px-4 py-2 text-sm font-medium text-zinc-600 hover:text-zinc-800"
              >
                Fermer
              </button>
            </slot>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>
