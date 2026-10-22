<script setup lang="ts">
defineProps<{ busy: boolean }>();
const emit = defineEmits<{ submit: [event: SubmitEvent] }>();
</script>

<template>
  <form
    action="/auth/start"
    method="post"
    class="space-y-5"
    :aria-busy="busy"
    @submit.prevent="emit('submit', $event)"
  >
    <UiInput
      id="work-email"
      name="email"
      type="email"
      autocomplete="username"
      :label="$t('authentication.email')"
      placeholder="you@company.com"
      required
      maxlength="254"
    />
    <UiButton type="submit" :loading="busy" class="w-full">{{
      $t("authentication.continue")
    }}</UiButton>
  </form>
</template>
