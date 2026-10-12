<script setup lang="ts">
import { FingerPrintIcon, ArrowPathIcon } from "@heroicons/vue/24/outline";
const { open, busy, error, step, connectionError, prepare, verify } =
  useSessionState();
const dialog = ref<HTMLDialogElement>();
watch(
  open,
  async (value) => {
    await nextTick();
    if (value && dialog.value && !dialog.value.open) {
      dialog.value.showModal();
      dialog.value.focus();
    } else if (!value && dialog.value?.open) dialog.value.close();
  },
  { flush: "post" },
);
onMounted(() => {
  if (open.value && dialog.value && !dialog.value.open) {
    dialog.value.showModal();
    dialog.value.focus();
  }
});
</script>

<template>
  <Teleport to="body">
    <p
      v-if="connectionError && !open"
      role="status"
      class="fixed bottom-5 left-1/2 z-[150] w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 rounded-xl border border-amber-200 bg-amber-50 px-5 py-4 text-sm text-amber-900 shadow-lg"
    >
      {{ connectionError }}
    </p>
    <dialog
      ref="dialog"
      aria-modal="true"
      aria-labelledby="passkey-setup-title"
      :aria-busy="busy"
      class="m-auto w-[calc(100%-3rem)] max-w-md rounded-2xl border border-amber-100 bg-white p-7 shadow-xl backdrop:bg-zinc-950/30 backdrop:backdrop-blur-sm"
      tabindex="-1"
      @cancel.prevent
    >
      <FingerPrintIcon class="mb-4 h-8 w-8 text-amber-600" aria-hidden="true" />
      <h2 id="passkey-setup-title" class="text-xl font-semibold text-zinc-900">
        Secure your account
      </h2>
      <p class="mt-3 text-sm leading-6 text-zinc-600">
        Your organization now requires a passkey.
        {{
          step?.operation === "authentication"
            ? "Verify with your existing key to continue."
            : "Create yours with your device or password manager to continue."
        }}
        You will stay on this page.
      </p>
      <p
        v-if="error"
        role="alert"
        class="mt-4 rounded-lg bg-amber-50 p-3 text-sm text-amber-900"
      >
        {{ error }}
      </p>
      <UtilsButton
        class="mt-6 w-full"
        :disabled="busy"
        @click="step ? verify() : prepare()"
      >
        <ArrowPathIcon
          v-if="busy"
          class="mr-2 h-4 w-4 animate-spin motion-reduce:animate-none"
          aria-hidden="true"
        />
        {{
          busy
            ? "Preparing…"
            : step?.operation === "authentication"
              ? "Verify passkey"
              : step
                ? "Create passkey"
                : "Retry setup"
        }}
      </UtilsButton>
      <form action="/auth/logout" method="post" class="mt-4 text-center">
        <button class="text-sm text-zinc-500 underline">Sign out</button>
      </form>
    </dialog>
  </Teleport>
</template>
