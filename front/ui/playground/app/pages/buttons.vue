<script setup lang="ts">
const variants = ["primary", "secondary", "ghost", "danger"] as const;
</script>
<template>
  <main class="p-6">
    <h1>Button state contracts</h1>
    <div class="flex flex-wrap gap-3">
      <UiButton
        v-for="variant in variants"
        :key="variant"
        :variant="variant"
        disabled
        >{{ variant }} disabled</UiButton
      >
      <fieldset disabled><UiButton>Inherited disabled</UiButton></fieldset>
      <UiButton loading>Pending action</UiButton>
    </div>
  </main>
</template>
