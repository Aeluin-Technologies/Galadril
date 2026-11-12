<script setup lang="ts">
const route = useRoute();
const file = computed(() => {
  const id = route.params.file;
  return typeof id === "string" && /^[a-f0-9]{32}$/.test(id) ? { id } : null;
});
</script>

<template>
  <main class="mx-auto max-w-3xl space-y-3 p-6">
    <h1 class="text-xl font-semibold">{{ $t("documentSigning.title") }}</h1>
    <NuxtLink to="/pengolo/library">{{ $t("documentSigning.back") }}</NuxtLink>
    <AiDocumentPreview
      v-if="file"
      :file="file"
      start-signing
      @close="navigateTo('/pengolo/library')"
    />
    <UiAlert v-else tone="danger">{{
      $t("documentSigning.unavailable")
    }}</UiAlert>
  </main>
</template>
