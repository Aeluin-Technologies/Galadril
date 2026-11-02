<script setup lang="ts">
const route = useRoute();
const identifier = computed(() =>
  typeof route.params.id === "string" ? route.params.id : "",
);
</script>
<template>
  <PipelineWorkspace :key="identifier" :pipeline-id="identifier" />
</template>
