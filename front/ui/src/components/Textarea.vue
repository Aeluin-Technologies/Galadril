<script setup lang="ts">
import { nextTick, onMounted, ref, watch } from "vue";
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
    autosize?: boolean;
    variant?: "field" | "composer";
  }>(),
  { variant: "field" },
);
const model = defineModel<string>();
const input = ref<HTMLTextAreaElement | null>(null);
async function resize() {
  await nextTick();
  if (!props.autosize || !input.value) return;
  input.value.style.height = "auto";
  input.value.style.height = `${Math.min(input.value.scrollHeight, 220)}px`;
}
watch(model, resize);
onMounted(resize);
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
    <textarea
      ref="input"
      v-bind="$attrs"
      :id="field.id"
      v-model="model"
      data-ui="textarea"
      :required="required"
      :disabled="disabled"
      :aria-invalid="field.invalid"
      :aria-describedby="field.describedby"
      :class="[
        'block w-full min-w-0 text-foreground placeholder:text-muted disabled:cursor-not-allowed aria-invalid:border-danger',
        variant === 'composer'
          ? 'text-ui-body min-h-6 resize-none border-0 bg-transparent px-1 py-1 leading-6 outline-none focus:outline-none'
          : 'text-ui min-h-24 resize-y rounded-control border border-border-control bg-surface px-3 py-2 transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus disabled:bg-surface-muted',
      ]"
    />
  </Field>
</template>
