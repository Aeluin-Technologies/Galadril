<script setup lang="ts">
await navigateTo(
  { path: "/settings/user", hash: "#email" },
  { replace: true, redirectCode: 302 },
);
</script>
<template><div /></template>
