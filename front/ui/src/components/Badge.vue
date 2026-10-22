<script setup lang="ts">
import {
  CheckCircleIcon,
  ExclamationTriangleIcon,
  InformationCircleIcon,
  XCircleIcon,
} from "@heroicons/vue/20/solid";
import type { Tone } from "../types";

withDefaults(defineProps<{ tone?: Tone }>(), { tone: "neutral" });
const themes: Record<Tone, string> = {
  neutral: "bg-surface-muted text-muted",
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
  <span
    data-ui="badge"
    :class="[
      'inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-ui-meta font-medium',
      themes[tone],
    ]"
    ><component :is="icons[tone]" class="size-3.5" aria-hidden="true" /><slot
  /></span>
</template>
