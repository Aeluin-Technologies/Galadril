<script setup lang="ts">
defineProps<{
  busy: boolean;
  ready: boolean;
  operation?: "registration" | "authentication";
}>();
const emit = defineEmits<{ submit: [] }>();
</script>

<template>
  <form class="space-y-5" :aria-busy="busy" @submit.prevent="emit('submit')">
    <UiAlert>{{ $t("authentication.passkey_privacy") }}</UiAlert>
    <UiButton type="submit" :loading="busy" :disabled="!ready" class="w-full">{{
      $t(
        operation === "registration"
          ? "authentication.create_passkey"
          : "authentication.use_passkey",
      )
    }}</UiButton>
    <NuxtLink
      to="/login"
      class="inline-block rounded-control text-ui text-info underline-offset-4 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-focus"
      >{{ $t("authentication.start_again") }}</NuxtLink
    >
  </form>
</template>
