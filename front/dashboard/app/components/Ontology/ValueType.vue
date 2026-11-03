<script setup lang="ts">
const props = defineProps<{ value: string }>();
const { t } = useI18n();
const target = ref<HTMLElement | null>(null);
const description = computed(() =>
  t(
    ["string", "integer", "boolean", "datetime", "json"].includes(props.value)
      ? `ontologyManager.valueTypes.${props.value}`
      : "ontologyManager.valueTypeCustom",
  ),
);
</script>
<template>
  <span
    ref="target"
    tabindex="0"
    class="inline-flex rounded-control focus-visible:outline focus-visible:outline-2 focus-visible:outline-focus"
    ><UiBadge>{{ value }}</UiBadge></span
  >
  <UiTooltip :target="target" :text="description" />
</template>
