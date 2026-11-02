<script setup lang="ts">
const route = useRoute();
const id = computed(() => String(route.params.id ?? ""));
const proposal = computed(() => String(route.params.proposal ?? ""));
</script>
<template>
  <PipelineReview
    :key="`${id}:${proposal}`"
    :pipeline-id="id"
    :proposal-id="proposal"
  />
</template>
