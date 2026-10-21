<script setup lang="ts">
import { computed, ref, useAttrs } from "vue";
import type { ButtonAction, ButtonVariant, ControlSize } from "../types";
import { recordAction } from "../action-metrics";
import Spinner from "./Spinner.vue";
import Tooltip from "./Tooltip.vue";

defineOptions({ inheritAttrs: false });
const props = withDefaults(
  defineProps<{
    variant?: ButtonVariant;
    size?: ControlSize;
    type?: "button" | "submit" | "reset";
    disabled?: boolean;
    loading?: boolean;
    loadingLabel?: string;
    pendingLabel?: string;
    tooltip?: string | false;
    action?: ButtonAction;
    iconOnly?: boolean;
    align?: "start" | "center";
  }>(),
  {
    variant: "primary",
    size: "md",
    type: "button",
    disabled: false,
    loading: false,
    iconOnly: false,
    align: "center",
    tooltip: undefined,
    pendingLabel: "Operation in progress",
  },
);
const emit = defineEmits<{
  click: [event: MouseEvent];
  actionError: [error: unknown];
}>();
const attrs = useAttrs();
const element = ref<HTMLButtonElement | null>(null);
const pending = ref(false);
const busy = computed(() => pending.value || props.loading);
const tooltipText = computed(() =>
  props.tooltip === false
    ? ""
    : (props.tooltip ??
      (typeof attrs["aria-label"] === "string" ? attrs["aria-label"] : "")),
);
const variants: Record<ButtonVariant, string> = {
  primary:
    "border-primary-active bg-primary text-on-primary shadow-panel enabled:aria-[disabled=false]:hover:bg-primary-hover enabled:aria-[disabled=false]:active:bg-primary-active enabled:aria-[disabled=false]:active:text-on-primary-active",
  secondary:
    "border-border-control bg-surface text-foreground shadow-panel enabled:aria-[disabled=false]:hover:bg-surface-muted enabled:aria-[disabled=false]:active:bg-surface-hover",
  ghost:
    "border-transparent bg-transparent text-muted enabled:aria-[disabled=false]:hover:bg-surface-muted enabled:aria-[disabled=false]:hover:text-foreground enabled:aria-[disabled=false]:active:bg-surface-hover aria-[current=page]:bg-primary-subtle aria-[current=page]:text-primary-foreground aria-[pressed=true]:bg-primary-subtle aria-[pressed=true]:text-primary-foreground",
  danger:
    "border-danger bg-surface text-danger enabled:aria-[disabled=false]:hover:bg-danger-subtle enabled:aria-[disabled=false]:active:bg-danger enabled:aria-[disabled=false]:active:text-surface",
};
const sizes: Record<ControlSize, string> = {
  sm: "min-h-8 px-3",
  md: "min-h-9 px-3",
  lg: "min-h-11 px-4",
};
async function click(event: MouseEvent) {
  if (props.disabled || busy.value) {
    event.preventDefault();
    event.stopImmediatePropagation();
    return;
  }
  if (!props.action) {
    emit("click", event);
    return;
  }
  event.preventDefault();
  pending.value = true;
  emit("click", event);
  const start = performance.now();
  try {
    await props.action(event);
    recordAction(start, "success");
  } catch (error: unknown) {
    recordAction(start, "failure");
    emit("actionError", error);
  } finally {
    pending.value = false;
  }
}
</script>

<template>
  <button
    ref="element"
    v-bind="attrs"
    data-ui="button"
    :type="type"
    :disabled="disabled"
    :aria-busy="busy"
    :aria-disabled="disabled || busy"
    :class="[
      'inline-flex shrink-0 items-center gap-2 rounded-control border text-ui font-medium transition-colors duration-fast focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus disabled:cursor-not-allowed disabled:opacity-50 [&>svg]:size-4 [&>svg]:shrink-0',
      variants[variant],
      iconOnly
        ? { sm: 'size-8 p-0', md: 'size-9 p-0', lg: 'size-11 p-0' }[size]
        : sizes[size],
      align === 'start' ? 'justify-start text-start' : 'justify-center',
      busy ? 'cursor-wait' : '',
      attrs.class,
    ]"
    @click="click"
  >
    <Spinner v-if="busy" />
    <slot v-else name="leading" />
    <span v-if="busy && loadingLabel">{{ loadingLabel }}</span>
    <slot v-else-if="!busy || !iconOnly" />
    <slot name="trailing" />
  </button>
  <span role="status" aria-live="polite" class="sr-only">{{
    busy ? pendingLabel : ""
  }}</span>
  <Tooltip v-if="tooltipText" :target="element" :text="tooltipText" />
</template>
