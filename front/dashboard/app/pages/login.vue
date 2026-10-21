<script setup lang="ts">
import { LockClosedIcon } from "@heroicons/vue/24/outline";
const { t, locale } = useI18n();
definePageMeta({ layout: false });
useHead({
  title: () => t("administration.copy.sign_in_galadril"),
  meta: [{ name: "robots", content: "noindex" }],
});
const {
  route,
  mfa,
  submitting,
  passkeyStep,
  localForm,
  loginError,
  stepContent,
  focusStep,
  submitPasskey,
  submitPassword,
  submitEmail,
} = useSignIn();
useHead({ htmlAttrs: { lang: () => locale.value } });
const title = computed(() =>
  mfa.value
    ? t(
        passkeyStep.value?.operation === "registration"
          ? "authentication.create_title"
          : "authentication.verify_title",
      )
    : t("authentication.title"),
);
const description = computed(() =>
  t(
    mfa.value
      ? passkeyStep.value?.operation === "registration"
        ? "authentication.create_description"
        : "authentication.verify_description"
      : localForm.value
        ? "authentication.password_description"
        : "authentication.email_description",
  ),
);
</script>

<template>
  <AuthenticationFrame>
    <div class="mb-7 space-y-2 text-center">
      <h1 id="login-title" class="text-2xl font-semibold tracking-tight">
        {{ title }}
      </h1>
      <p class="text-ui leading-relaxed text-muted">
        {{ description }}
      </p>
    </div>
    <UiAlert
      v-if="route.query.expired === '1' || route.query.recovery === '1'"
      class="mb-5"
      tone="warning"
      >{{
        $t(
          route.query.recovery === "1"
            ? "authentication.recovery"
            : "authentication.expired",
        )
      }}</UiAlert
    >
    <UiAlert v-if="loginError" class="mb-5" tone="danger">{{
      loginError
    }}</UiAlert>
    <Transition name="auth-step" mode="out-in" @after-enter="focusStep">
      <div
        :key="mfa ? 'passkey' : localForm ? 'password' : 'email'"
        ref="stepContent"
      >
        <AuthenticationPasskeyForm
          v-if="mfa"
          :busy="submitting"
          :ready="!!passkeyStep"
          :operation="passkeyStep?.operation"
          @submit="submitPasskey"
        />
        <AuthenticationPasswordForm
          v-else-if="localForm"
          :form="localForm"
          :busy="submitting"
          @submit="submitPassword"
        />
        <AuthenticationEmailForm
          v-else
          :busy="submitting"
          @submit="submitEmail"
        />
      </div>
    </Transition>
    <div class="mt-6 flex items-start gap-2 border-t border-border pt-5">
      <LockClosedIcon
        class="mt-0.5 size-4 shrink-0 text-muted"
        aria-hidden="true"
      />
      <p class="text-ui-meta leading-relaxed text-muted">
        {{ $t("authentication.credentials_notice") }}
      </p>
    </div>
  </AuthenticationFrame>
</template>

<style scoped>
.auth-step-enter-active,
.auth-step-leave-active {
  transition: opacity var(--ui-duration-fast) ease;
}
.auth-step-enter-from,
.auth-step-leave-to {
  opacity: 0;
}
@media (prefers-reduced-motion: reduce) {
  .auth-step-enter-active,
  .auth-step-leave-active {
    transition: none;
  }
}
</style>
