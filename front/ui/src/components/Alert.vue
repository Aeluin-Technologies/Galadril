<script setup lang="ts">
import {
  CheckCircleIcon,
  ExclamationTriangleIcon,
  InformationCircleIcon,
  XCircleIcon,
} from "@heroicons/vue/20/solid";
import type { Tone } from "../types";

withDefaults(defineProps<{ title?: string; tone?: Tone }>(), { tone: "info" });
const themes: Record<Tone, string> = {
  neutral: "bg-surface-muted text-foreground",
  info: "bg-info-subtle text-info",
  success: "bg-success-subtle text-success",
  warning: "bg-warning-subtle text-warning",
  danger: "bg-danger-subtle text-danger",
};
const icons = {
  neutral: InformationCircleIcon,
  info: InformationCircleIcon,
  success: CheckCircleIcon,
  warning: ExclamationTriangleIcon,
  danger: XCircleIcon,
};
</script>

<template>
  <div
    data-ui="alert"
    :role="tone === 'danger' ? 'alert' : 'status'"
    :class="[
      'flex items-start gap-3 rounded-panel border border-current/20 p-4 text-ui',
      themes[tone],
    ]"
  >
    <component
      :is="icons[tone]"
      class="mt-0.5 size-5 shrink-0"
      aria-hidden="true"
    />
    <div>
      <p v-if="title" class="font-semibold">{{ title }}</p>
      <div v-if="$slots.default" :class="title ? 'mt-1' : undefined">
        <slot />
      </div>
    </div>
  </div>
</template>
