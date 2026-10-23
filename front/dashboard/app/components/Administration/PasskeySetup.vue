<script setup lang="ts">
const { open, busy, error, step, connectionError, prepare, verify } =
  useSessionState();
</script>

<template>
  <Teleport to="body">
    <UiAlert
      v-if="connectionError && !open"
      tone="warning"
      class="fixed bottom-5 left-1/2 z-[150] w-[calc(100%-2rem)] max-w-lg -translate-x-1/2"
      >{{ connectionError }}</UiAlert
    >
  </Teleport>
  <UiDialog
    :open="open"
    :dismissible="false"
    :title="$t('authentication.secure_account')"
    :aria-busy="busy"
  >
    <p class="text-ui leading-relaxed text-muted">
      {{ $t("authentication.enrollment_notice") }}
      {{
        $t(
          step?.operation === "authentication"
            ? "authentication.verify_description"
            : "authentication.create_description",
        )
      }}
      {{ $t("authentication.stay_on_page") }}
    </p>
    <UiAlert v-if="error" tone="danger" class="mt-4">{{ error }}</UiAlert>
    <UiButton
      class="mt-6 w-full"
      :loading="busy"
      @click="step ? verify() : prepare()"
      >{{
        $t(
          step?.operation === "authentication"
            ? "authentication.verify_passkey"
            : step
              ? "authentication.create_passkey"
              : "authentication.retry_setup",
        )
      }}</UiButton
    >
    <template #footer>
      <form action="/auth/logout" method="post">
        <UiButton type="submit" variant="ghost">{{
          $t("authentication.sign_out")
        }}</UiButton>
      </form>
    </template>
  </UiDialog>
</template>
