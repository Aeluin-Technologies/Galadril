<script setup lang="ts">
import type { Component } from "vue";
import type { ButtonAction, ButtonVariant, ControlSize } from "../types";
import Button from "./Button.vue";

defineOptions({ inheritAttrs: false });
withDefaults(
  defineProps<{
    label: string;
    icon: Component;
    tooltip?: string | false;
    variant?: ButtonVariant;
    size?: ControlSize;
    disabled?: boolean;
    loading?: boolean;
    action?: ButtonAction;
  }>(),
  { variant: "ghost", tooltip: undefined },
);
const emit = defineEmits<{
  click: [event: MouseEvent];
  actionError: [error: unknown];
}>();
</script>

<template>
  <Button
    v-bind="$attrs"
    :aria-label="label"
    :tooltip="tooltip ?? label"
    :variant="variant"
    :size="size"
    :disabled="disabled"
    :loading="loading"
    :action="action"
    icon-only
    @click="emit('click', $event)"
    @action-error="emit('actionError', $event)"
  >
    <component :is="icon" aria-hidden="true" />
  </Button>
</template>
