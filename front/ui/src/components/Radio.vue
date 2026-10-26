<script setup lang="ts">
defineOptions({ inheritAttrs: false });
defineProps<{ value: string; name?: string; disabled?: boolean }>();
const model = defineModel<string>();
</script>

<template>
  <span class="inline-flex shrink-0" data-ui="radio">
    <input
      v-bind="$attrs"
      v-model="model"
      type="radio"
      :name="name"
      :value="value"
      :disabled="disabled"
      class="mt-0.5 size-4 shrink-0 accent-primary-active focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus disabled:cursor-not-allowed"
    />
  </span>
</template>
