<script setup lang="ts">
const route = useRoute();
const id = computed(() =>
  typeof route.params.id === "string" ? route.params.id : "",
);
</script>
<template><OntologyWorkspace :key="id" :ontology-id="id" /></template>
