<script setup lang="ts">
import { UiDialog, UiButton } from "@galadril/ui/components";

withDefaults(defineProps<{ isOpen: boolean; title?: string }>(), { title: "" });
const emit = defineEmits<{ close: [] }>();
</script>

<template>
  <UiDialog
    :open="isOpen"
    :title="title"
    :close-label="$t('actions.close')"
    @close="emit('close')"
  >
    <slot />
    <template #footer
      ><slot name="footer"
        ><UiButton variant="secondary" @click="emit('close')">{{
          $t("actions.close")
        }}</UiButton></slot
      ></template
    >
  </UiDialog>
</template>
