<script setup lang="ts" generic="T extends string | number | boolean = string">
import { computed, ref } from "vue";
import {
  SelectRoot,
  SelectTrigger,
  SelectValue,
  SelectPortal,
  SelectContent,
  SelectViewport,
  SelectItem,
  SelectItemText,
  SelectItemIndicator,
  SelectScrollUpButton,
  SelectScrollDownButton,
} from "reka-ui";
import {
  CheckIcon,
  ChevronDownIcon,
  ChevronUpIcon,
} from "@heroicons/vue/20/solid";
import type { SelectOption } from "../types";
import Field from "./Field.vue";

defineOptions({ inheritAttrs: false });
const props = withDefaults(
  defineProps<{
    id?: string;
    name?: string;
    label?: string;
    placeholder?: string;
    help?: string;
    error?: string;
    required?: boolean;
    disabled?: boolean;
    variant?: "field" | "inline";
    options: readonly SelectOption<T>[];
  }>(),
  { variant: "field" },
);
const model = defineModel<T>();
const selected = computed({
  get: () => (model.value === undefined ? "" : String(model.value)),
  set: (value: string) => {
    const option = props.options.find(
      (option) => (String(option.value) || "__ui_empty__") === value,
    );
    if (option) model.value = option.value;
  },
});
const placeholderText = computed(
  () =>
    props.placeholder ??
    props.options.find((option) => option.value === "")?.label,
);
const selectedLabel = computed(
  () =>
    props.options.find((option) => String(option.value) === selected.value)
      ?.label,
);
const open = ref(false);
const anchor = ref<HTMLElement | null>(null);
// Native modal top layers make portals outside the dialog inert.
const portalTarget = computed(
  () => anchor.value?.closest("dialog") ?? undefined,
);
</script>

<template>
  <Field
    :id="id"
    :label="label"
    :help="help"
    :error="error"
    :required="required"
    v-slot="field"
  >
    <div ref="anchor">
      <SelectRoot
        v-model="selected"
        v-model:open="open"
        :name="name"
        :required="required"
        :disabled="disabled"
      >
        <SelectTrigger
          v-bind="$attrs"
          :id="field.id"
          data-ui="select"
          :aria-invalid="field.invalid"
          :aria-describedby="field.describedby"
          :class="[
            'group flex h-9 w-full appearance-none items-center justify-between gap-3 rounded-control border text-start text-ui text-foreground transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus disabled:cursor-not-allowed aria-invalid:border-danger',
            variant === 'inline'
              ? 'border-transparent bg-transparent px-2 hover:bg-surface-muted data-[state=open]:bg-surface-muted'
              : 'border-border-control bg-surface px-3 hover:border-primary-foreground disabled:bg-surface-muted data-[state=open]:border-primary-foreground',
          ]"
        >
          <SelectValue
            :placeholder="placeholderText"
            class="min-w-0 truncate"
            >{{ selectedLabel }}</SelectValue
          >
          <ChevronDownIcon
            class="size-4 shrink-0 text-muted transition-transform group-data-[state=open]:rotate-180"
            aria-hidden="true"
          />
        </SelectTrigger>
        <SelectPortal :to="portalTarget">
          <SelectContent
            @escape-key-down.prevent="open = false"
            data-ui="select-content"
            position="popper"
            align="start"
            :side-offset="6"
            :collision-padding="8"
            class="z-50 max-h-[var(--reka-select-content-available-height)] min-w-[var(--reka-select-trigger-width)] max-w-[calc(100vw-1rem)] overflow-hidden rounded-panel border border-border bg-surface p-1 text-ui text-foreground shadow-popover"
          >
            <SelectScrollUpButton
              class="flex h-6 items-center justify-center text-muted"
              ><ChevronUpIcon class="size-4" aria-hidden="true"
            /></SelectScrollUpButton>
            <SelectViewport class="p-0.5">
              <SelectItem
                v-for="option in options"
                :key="String(option.value)"
                :value="String(option.value) || '__ui_empty__'"
                :disabled="option.disabled"
                class="relative flex min-h-9 cursor-default select-none items-center gap-3 rounded-control py-2 ps-3 pe-9 outline-none data-[highlighted]:bg-primary-subtle data-[highlighted]:text-primary-foreground data-[state=checked]:font-medium data-[disabled]:text-muted data-[disabled]:opacity-60"
              >
                <SelectItemText>{{ option.label }}</SelectItemText>
                <SelectItemIndicator
                  class="absolute end-2 flex items-center text-primary-foreground"
                  ><CheckIcon class="size-4" aria-hidden="true"
                /></SelectItemIndicator>
              </SelectItem>
            </SelectViewport>
            <SelectScrollDownButton
              class="flex h-6 items-center justify-center text-muted"
              ><ChevronDownIcon class="size-4" aria-hidden="true"
            /></SelectScrollDownButton>
          </SelectContent>
        </SelectPortal>
      </SelectRoot>
    </div>
  </Field>
</template>
