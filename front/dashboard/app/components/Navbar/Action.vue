<script setup lang="ts">
import { XMarkIcon } from "@heroicons/vue/24/outline";

defineProps({
  isEmpty: {
    type: Boolean,
    default: false,
  },
});

const emit = defineEmits(["close"]);
</script>

<template>
  <aside
    v-if="!isEmpty"
    class="w-80 bg-stone-50 border-l border-zinc-200 flex flex-col h-full shadow-lg relative z-30"
  >
    <div class="p-5 border-b border-zinc-200 bg-white">
      <div class="flex items-center justify-between mb-3">
        <div>
          <slot name="header-badge"></slot>
        </div>
        <button
          @click="emit('close')"
          class="text-zinc-400 hover:text-amber-700 hover:bg-amber-100 p-1 rounded-full transition cursor-pointer"
        >
          <XMarkIcon class="w-5 h-5" />
        </button>
      </div>
      <slot name="header-content"></slot>
    </div>

    <div class="p-5 space-y-4 flex-1 overflow-y-auto text-xs text-zinc-800">
      <slot></slot>
    </div>

    <div
      v-if="$slots.footer"
      class="mt-auto p-5 border-t border-zinc-200 bg-white"
    >
      <slot name="footer"></slot>
    </div>
  </aside>

  <aside
    v-else
    class="w-80 bg-stone-50 border-l border-zinc-200 flex items-center justify-center h-full"
  >
    <slot name="empty">
      <p class="text-zinc-400 text-xs italic">
        {{ $t("details_panel.empty_state") }}
      </p>
    </slot>
  </aside>
</template>
