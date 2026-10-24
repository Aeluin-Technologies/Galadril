<script setup lang="ts">
const { t } = useI18n();
useHead({
  title: () => t("email.pageTitle"),
  htmlAttrs: { lang: "en" },
  meta: [{ name: "referrer", content: "no-referrer" }],
});
</script>
<template><EmailWorkspace /></template>
