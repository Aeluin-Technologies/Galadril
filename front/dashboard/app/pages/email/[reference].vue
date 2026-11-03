<script setup lang="ts">
const route = useRoute();
const reference = computed(() =>
  typeof route.params.reference === "string" ? route.params.reference : "",
);
</script>
<template>
  <EmailReference :reference="reference" @close="navigateTo('/pengolo')" />
</template>
