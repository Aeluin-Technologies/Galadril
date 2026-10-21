<script setup lang="ts" generic="T extends string | number = string">
import { computed, ref, useAttrs } from "vue";
import Field from "./Field.vue";

defineOptions({ inheritAttrs: false });
const props = withDefaults(
  defineProps<{
    id?: string;
    label?: string;
    help?: string;
    error?: string;
    required?: boolean;
    disabled?: boolean;
    type?: string;
    value?: T;
  }>(),
  { type: "text" },
);
const model = defineModel<T>();
const attrs = useAttrs();
const describedBy = computed(() =>
  typeof attrs["aria-describedby"] === "string"
    ? attrs["aria-describedby"]
    : undefined,
);
const bound = computed({
  get: () => model.value ?? props.value,
  set: (value: T | undefined) => {
    model.value = value;
  },
});
const input = ref<HTMLInputElement | null>(null);
defineExpose({
  focus: () => input.value?.focus(),
  select: () => input.value?.select(),
});
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
    <input
      ref="input"
      v-bind="$attrs"
      :id="field.id"
      v-model="bound"
      data-ui="input"
      :type="type"
      :required="required"
      :disabled="disabled"
      :aria-invalid="
        field.invalid ||
        attrs['aria-invalid'] === true ||
        attrs['aria-invalid'] === 'true'
      "
      :aria-describedby="field.describedby ?? describedBy"
      class="h-9 w-full min-w-0 rounded-control border border-border-control bg-surface px-3 text-ui text-foreground transition-colors duration-fast placeholder:text-muted focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus disabled:cursor-not-allowed disabled:bg-surface-muted aria-invalid:border-danger"
    />
  </Field>
</template>
