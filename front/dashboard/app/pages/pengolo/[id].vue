<script setup lang="ts">
const route = useRoute();
const id = computed(() =>
  typeof route.params.id === "string" ? route.params.id : undefined,
);
</script>
<template><AiWorkspace view="chat" :chat-id="id" /></template>
