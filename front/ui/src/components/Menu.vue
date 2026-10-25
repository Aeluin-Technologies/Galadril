<script setup lang="ts">
import { onDeactivated, ref } from "vue";
import {
  DropdownMenuRoot,
  DropdownMenuTrigger,
  DropdownMenuPortal,
  DropdownMenuContent,
  DropdownMenuItem,
} from "reka-ui";
import { ChevronDownIcon } from "@heroicons/vue/20/solid";
import type { Component } from "vue";
import type { ButtonVariant, MenuItem } from "../types";
import Button from "./Button.vue";
import IconButton from "./IconButton.vue";

defineOptions({ inheritAttrs: false });
withDefaults(
  defineProps<{
    label: string;
    items: readonly MenuItem[];
    variant?: ButtonVariant;
    iconOnly?: boolean;
    triggerIcon?: Component;
    disabled?: boolean;
  }>(),
  { variant: "primary" },
);
const emit = defineEmits<{ select: [id: string] }>();
const open = ref(false);
onDeactivated(() => {
  open.value = false;
});
</script>

<template>
  <DropdownMenuRoot v-model:open="open" :modal="false">
    <DropdownMenuTrigger as-child>
      <IconButton
        v-bind="$attrs"
        v-if="iconOnly && triggerIcon"
        :label="label"
        :icon="triggerIcon"
        :variant="variant"
        :disabled="disabled"
      />
      <Button v-else v-bind="$attrs" :variant="variant" :disabled="disabled">
        <slot name="icon" />
        {{ label }}
        <ChevronDownIcon aria-hidden="true" />
      </Button>
    </DropdownMenuTrigger>
    <DropdownMenuPortal>
      <DropdownMenuContent
        :aria-label="label"
        align="start"
        :side-offset="6"
        :collision-padding="8"
        class="z-50 grid max-h-[var(--reka-dropdown-menu-content-available-height)] w-72 max-w-[calc(100vw-1rem)] gap-1 overflow-y-auto rounded-panel border border-border bg-surface p-1.5 text-ui text-foreground shadow-popover"
      >
        <DropdownMenuItem
          v-for="item in items"
          :key="item.id"
          :disabled="item.disabled"
          :text-value="item.label"
          :data-tone="item.tone"
          :class="[
            'flex min-h-9 cursor-default select-none items-center gap-3 rounded-control px-3 py-2 outline-none data-[disabled]:text-muted data-[disabled]:opacity-60',
            item.tone === 'danger'
              ? 'text-danger data-[highlighted]:bg-danger data-[highlighted]:text-surface'
              : 'data-[highlighted]:bg-primary-subtle data-[highlighted]:text-primary-foreground',
          ]"
          @select="emit('select', item.id)"
        >
          <component
            v-if="item.icon"
            :is="item.icon"
            class="size-4 shrink-0"
            aria-hidden="true"
          />
          <span class="min-w-0 text-start">
            <span class="block">{{ item.label }}</span>
            <span v-if="item.description" class="block text-xs font-normal">{{
              item.description
            }}</span>
          </span>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenuPortal>
  </DropdownMenuRoot>
</template>
