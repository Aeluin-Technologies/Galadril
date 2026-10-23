<script setup lang="ts">
import { UiButton } from "@galadril/ui/components";
import type { ButtonVariant } from "@galadril/ui/types";

defineOptions({ inheritAttrs: false });
withDefaults(
  defineProps<{ variant?: ButtonVariant; tooltip?: string | false }>(),
  { variant: "primary", tooltip: undefined },
);
</script>

<template>
  <UiButton
    v-bind="$attrs"
    :variant="variant"
    size="sm"
    :pending-label="$t('actions.working')"
    :tooltip="
      tooltip ?? (typeof $attrs.title === 'string' ? $attrs.title : undefined)
    "
    :title="undefined"
  >
    <template v-if="$slots.leading" #leading><slot name="leading" /></template>
    <slot />
    <template v-if="$slots.trailing" #trailing
      ><slot name="trailing"
    /></template>
  </UiButton>
</template>
