<script setup lang="ts">
import { computed, useId } from "vue";

const props = defineProps<{
  id?: string;
  label?: string;
  help?: string;
  error?: string;
  required?: boolean;
}>();
const generated = useId();
const fieldId = computed(() => props.id ?? generated);
const description = computed(() =>
  props.error
    ? `${fieldId.value}-error`
    : props.help
      ? `${fieldId.value}-help`
      : undefined,
);
</script>

<template>
  <div v-if="label" data-ui="field" class="grid gap-2 text-ui">
    <label :for="fieldId" class="font-medium text-foreground"
      >{{ label
      }}<span v-if="required" aria-hidden="true" class="ms-1 text-danger"
        >*</span
      ></label
    >
    <slot :id="fieldId" :describedby="description" :invalid="Boolean(error)" />
    <p v-if="error" :id="`${fieldId}-error`" class="text-danger">{{ error }}</p>
    <p v-else-if="help" :id="`${fieldId}-help`" class="text-muted">
      {{ help }}
    </p>
  </div>
  <slot
    v-else
    :id="fieldId"
    :describedby="description"
    :invalid="Boolean(error)"
  />
</template>
