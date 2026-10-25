<script setup lang="ts">
import { computed, reactive, ref } from "vue";
import {
  ComboboxRoot,
  ComboboxAnchor,
  ComboboxInput,
  ComboboxTrigger,
  ComboboxPortal,
  ComboboxContent,
  ComboboxViewport,
  ComboboxItem,
  ComboboxItemIndicator,
  ComboboxEmpty,
} from "reka-ui";
import {
  CheckIcon,
  ChevronDownIcon,
  InformationCircleIcon,
} from "@heroicons/vue/20/solid";
import Field from "./Field.vue";
import Tooltip from "./Tooltip.vue";
const props = withDefaults(
  defineProps<{
    label: string;
    searchLabel: string;
    emptyLabel: string;
    options: readonly {
      value: string;
      label: string;
      description?: string;
      disabled?: boolean;
    }[];
    multiple?: boolean;
    disabled?: boolean;
  }>(),
  { multiple: false },
);
const model = defineModel<string | string[]>({ required: true });
const query = ref("");
const open = ref(false);
const anchor = ref<HTMLElement | null>(null);
const hints = reactive(new Map<string, HTMLElement>());
const portalTarget = computed(
  () => anchor.value?.closest("dialog") ?? undefined,
);
const filtered = computed(() =>
  props.options.filter((option) =>
    (option.label + " " + (option.description ?? ""))
      .toLocaleLowerCase()
      .includes(query.value.trim().toLocaleLowerCase()),
  ),
);
const labelText = computed(
  () =>
    props.options
      .filter((option) =>
        Array.isArray(model.value)
          ? model.value.includes(option.value)
          : option.value === model.value,
      )
      .map((option) => option.label)
      .join(", ") || props.label,
);
function update(value: unknown) {
  if (
    props.multiple &&
    Array.isArray(value) &&
    value.every((item) => typeof item === "string")
  )
    model.value = value;
  else if (!props.multiple && typeof value === "string") model.value = value;
}
function hint(id: string, element: unknown) {
  if (element instanceof HTMLElement && hints.get(id) !== element)
    hints.set(id, element);
  else if (element === null) hints.delete(id);
}
</script>
<template>
  <Field :label="label" v-slot="field">
    <div ref="anchor">
      <ComboboxRoot
        v-model:open="open"
        ignore-filter
        :model-value="model"
        :multiple="multiple"
        :disabled="disabled"
        @update:model-value="update"
        @update:open="query = ''"
      >
        <ComboboxAnchor as-child>
          <ComboboxTrigger
            role="combobox"
            :id="field.id"
            :aria-label="label"
            class="flex min-h-9 w-full items-center justify-between gap-2 rounded-control border border-border-control bg-surface px-3 text-start text-ui text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-focus disabled:opacity-50"
          >
            <span class="truncate">{{ labelText }}</span
            ><ChevronDownIcon class="size-4 shrink-0" aria-hidden="true" />
          </ComboboxTrigger>
        </ComboboxAnchor>
        <ComboboxPortal :to="portalTarget">
          <ComboboxContent
            @escape-key-down.prevent.stop="open = false"
            position="popper"
            :side-offset="6"
            :collision-padding="8"
            class="z-50 w-80 max-w-[calc(100vw-1rem)] rounded-panel border border-border bg-surface p-2 text-ui text-foreground shadow-popover"
          >
            <ComboboxInput
              v-model="query"
              :aria-label="searchLabel"
              :placeholder="searchLabel"
              class="mb-2 h-9 w-full rounded-control border border-border-control bg-surface px-3 focus-visible:outline focus-visible:outline-2 focus-visible:outline-focus"
            />
            <ComboboxViewport class="max-h-64 overflow-auto">
              <ComboboxEmpty class="p-3 text-muted">{{
                emptyLabel
              }}</ComboboxEmpty>
              <ComboboxItem
                v-for="option in filtered"
                :key="option.value"
                :value="option.value"
                :disabled="option.disabled"
                :text-value="option.label"
                class="relative flex min-h-9 cursor-default select-none items-center gap-2 rounded-control py-2 ps-3 pe-9 outline-none data-[highlighted]:bg-primary-subtle data-[highlighted]:text-primary-foreground"
              >
                <span>{{ option.label }}</span>
                <span
                  v-if="option.description"
                  :ref="(el) => hint(option.value, el)"
                  aria-hidden="true"
                  ><InformationCircleIcon class="size-4 text-muted"
                /></span>
                <span v-if="option.description" class="sr-only">{{
                  option.description
                }}</span>
                <ComboboxItemIndicator class="absolute end-2"
                  ><CheckIcon class="size-4" aria-hidden="true"
                /></ComboboxItemIndicator>
              </ComboboxItem>
            </ComboboxViewport>
          </ComboboxContent>
        </ComboboxPortal>
      </ComboboxRoot>
      <Tooltip
        v-for="option in filtered.filter((item) => item.description)"
        :key="option.value"
        :target="hints.get(option.value) ?? null"
        :text="option.description ?? ''"
      />
    </div>
  </Field>
</template>
