<script setup lang="ts">
const route = useRoute();
const id = computed(() => String(route.params.id ?? ""));
const revision = computed(() => String(route.params.revision ?? ""));
const branch = computed(() =>
  typeof route.query.branch === "string" ? route.query.branch : "main",
);
</script>
<template>
  <PipelineRevision
    :key="`${id}:${revision}:${branch}`"
    :pipeline-id="id"
    :revision-id="revision"
    :branch="branch"
  />
</template>
