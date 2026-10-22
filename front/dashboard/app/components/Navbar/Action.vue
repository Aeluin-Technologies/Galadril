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
    class="w-80 bg-canvas border-l border-border flex flex-col h-full shadow-lg relative z-30"
  >
    <div class="p-5 border-b border-border bg-surface">
      <div class="flex items-center justify-between mb-3">
        <div>
          <slot name="header-badge"></slot>
        </div>
        <UiIconButton
          @click="emit('close')"
          variant="ghost"
          :icon="XMarkIcon"
          :label="$t('ui.actions.close')"
        />
      </div>
      <slot name="header-content"></slot>
    </div>

    <div class="p-5 space-y-4 flex-1 overflow-y-auto text-xs text-foreground">
      <slot></slot>
    </div>

    <div
      v-if="$slots.footer"
      class="mt-auto p-5 border-t border-border bg-surface"
    >
      <slot name="footer"></slot>
    </div>
  </aside>

  <aside
    v-else
    class="w-80 bg-canvas border-l border-border flex items-center justify-center h-full"
  >
    <slot name="empty">
      <p class="text-muted text-xs italic">
        {{ $t("details_panel.empty_state") }}
      </p>
    </slot>
  </aside>
</template>
