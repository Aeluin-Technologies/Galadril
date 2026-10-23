<script setup lang="ts" generic="T extends boolean | string[] = boolean">
import { computed, useId } from "vue";

defineOptions({ inheritAttrs: false });
const props = defineProps<{
  id?: string;
  label?: string;
  help?: string;
  disabled?: boolean;
  checked?: T;
}>();
const model = defineModel<T>();
const bound = computed({
  get: () => model.value ?? props.checked,
  set: (value: T | undefined) => {
    model.value = value;
  },
});
const generated = useId();
const id = computed(() => props.id ?? generated);
</script>

<template>
  <component
    :is="label ? 'div' : 'span'"
    data-ui="checkbox"
    class="inline-flex items-start gap-3 text-ui"
  >
    <input
      v-bind="$attrs"
      :id="id"
      v-model="bound"
      type="checkbox"
      :disabled="disabled"
      :aria-describedby="help ? `${id}-help` : undefined"
      class="mt-0.5 size-4 shrink-0 accent-primary-active focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
    />
    <div v-if="label">
      <label :for="id" class="font-medium text-foreground">{{ label }}</label>
      <p v-if="help" :id="`${id}-help`" class="mt-1 text-muted">{{ help }}</p>
    </div>
  </component>
</template>
