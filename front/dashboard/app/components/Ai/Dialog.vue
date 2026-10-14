<script setup lang="ts">
const props = defineProps<{ open: boolean; title: string }>();
const emit = defineEmits<{ close: [] }>();
const dialog = ref<HTMLDialogElement | null>(null);
const titleId = useId();
watch(
  () => props.open,
  async (open) => {
    await nextTick();
    if (open && !dialog.value?.open) dialog.value?.showModal();
    else if (!open) dialog.value?.close();
  },
  { immediate: true },
);
</script>

<template>
  <Teleport to="body">
    <dialog
      v-if="open"
      ref="dialog"
      class="p-dialog"
      :aria-labelledby="titleId"
      @cancel.prevent="emit('close')"
      @click="$event.target === dialog && emit('close')"
    >
      <header>
        <h2 :id="titleId">{{ title }}</h2>
        <UtilsButton
          variant="ghost"
          type="button"
          class="p-icon"
          aria-label="Close dialog"
          @click="emit('close')"
        >
          ×
        </UtilsButton>
      </header>
      <div class="p-dialog-body"><slot /></div>
      <footer v-if="$slots.footer"><slot name="footer" /></footer>
    </dialog>
  </Teleport>
</template>
