<script setup lang="ts">
import { ArrowRightIcon, LockClosedIcon } from "@heroicons/vue/24/outline";
import {
  beginSignIn,
  completePasswordSignIn,
  completePasskeySignIn,
  requestPasskey,
  passkeyFailure,
} from "~/utils/auth-navigation.mjs";
import {
  startRegistration,
  startAuthentication,
  type PublicKeyCredentialCreationOptionsJSON,
  type PublicKeyCredentialRequestOptionsJSON,
} from "@simplewebauthn/browser";
definePageMeta({ layout: false });
useHead({
  title: "Sign in · Galadril",
  meta: [{ name: "robots", content: "noindex" }],
});
const route = useRoute();
const submitting = ref(false);
const mfa = computed(() => route.query.mfa === "1");
type PasskeyStep =
  | {
      operation: "registration";
      options: PublicKeyCredentialCreationOptionsJSON;
    }
  | {
      operation: "authentication";
      options: PublicKeyCredentialRequestOptionsJSON;
    };
const passkeyStep = ref<PasskeyStep>();
watch(
  mfa,
  async (required, _previous, onCleanup) => {
    passkeyStep.value = undefined;
    if (!required) return;
    const controller = new AbortController();
    onCleanup(() => controller.abort());
    try {
      passkeyStep.value = await $fetch<PasskeyStep>("/auth/mfa/options", {
        signal: controller.signal,
      });
    } catch {
      if (!controller.signal.aborted)
        loginError.value = "This verification expired. Please start again.";
    }
  },
  { immediate: true },
);
async function submitPasskey() {
  if (submitting.value) return;
  submitting.value = true;
  loginError.value = undefined;
  let stage = "authenticator";
  try {
    const proof = await requestPasskey(passkeyStep.value, {
      startRegistration,
      startAuthentication,
    });
    stage = "verification";
    await navigateTo(
      await completePasskeySignIn(proof, window.location.origin),
    );
  } catch (error) {
    const failure = passkeyFailure(error);
    loginError.value = failure.message;
    console.warn(
      JSON.stringify({
        event: "auth.passkey.rejected",
        stage,
        reason: failure.reason,
      }),
    );
    passkeyStep.value = undefined;
    try {
      passkeyStep.value = await $fetch<PasskeyStep>("/auth/mfa/options");
    } catch {
      loginError.value = "This verification expired. Please start again.";
    }
  } finally {
    submitting.value = false;
  }
}
interface IdentityNode {
  type: string;
  attributes: {
    name: string;
    type: string;
    value?: string;
    required?: boolean;
    disabled?: boolean;
  };
  meta: { label?: { text: string } };
}
interface IdentityForm {
  action: string;
  method: string;
  nodes: IdentityNode[];
  messages?: { text: string; type: string }[];
}
const localForm = ref<IdentityForm>();
const loginError = ref<string>();
const stepContent = ref<HTMLElement>();

async function loadLocalForm(flow: string, signal?: AbortSignal) {
  localForm.value = await $fetch<IdentityForm>("/auth/local/flow", {
    query: { flow },
    signal,
  });
}

watch(
  () => route.query.flow,
  async (flow, _previous, onCleanup) => {
    loginError.value = undefined;
    if (typeof flow !== "string") {
      localForm.value = undefined;
      return;
    }
    const controller = new AbortController();
    onCleanup(() => controller.abort());
    try {
      await loadLocalForm(flow, controller.signal);
    } catch {
      if (!controller.signal.aborted)
        loginError.value = "This sign-in attempt expired. Please start again.";
    }
  },
  { immediate: true },
);

function focusStep() {
  stepContent.value
    ?.querySelector<HTMLInputElement>(
      'input[type="password"], input[name="email"]',
    )
    ?.focus({ preventScroll: true });
}

async function submitEmail(event: SubmitEvent) {
  if (submitting.value || !(event.target instanceof HTMLFormElement)) return;
  submitting.value = true;
  loginError.value = undefined;
  try {
    const email = new FormData(event.target).get("email");
    if (typeof email !== "string") throw new Error("Missing email");
    const destination = await beginSignIn(email, window.location.origin);
    await navigateTo(destination.path, { external: destination.external });
  } catch {
    loginError.value = "Sign-in is unavailable. Please try again.";
    console.warn(JSON.stringify({ event: "auth.navigation.rejected" }));
  } finally {
    submitting.value = false;
  }
}

async function submitPassword(event: SubmitEvent) {
  if (submitting.value || !(event.target instanceof HTMLFormElement)) return;
  const form = event.target;
  // Capture the submitter before changing UI state so Kratos receives its method.
  const fields = new FormData(form, event.submitter);
  submitting.value = true;
  loginError.value = undefined;
  try {
    const body = new URLSearchParams();
    for (const [name, value] of fields) {
      if (typeof value !== "string") throw new Error("Invalid login field");
      body.append(name, value);
    }
    const destination = await completePasswordSignIn(
      form.action,
      body,
      window.location.origin,
    );
    if (destination === route.fullPath && typeof route.query.flow === "string")
      await loadLocalForm(route.query.flow);
    else await navigateTo(destination);
  } catch {
    loginError.value = "Sign-in could not be completed. Please try again.";
    console.warn(JSON.stringify({ event: "auth.navigation.rejected" }));
  } finally {
    const password = form.querySelector<HTMLInputElement>(
      'input[type="password"]',
    );
    if (password) password.value = "";
    submitting.value = false;
  }
}
</script>

<template>
  <div
    class="relative isolate min-h-dvh overflow-hidden bg-stone-50 font-sans text-zinc-900 antialiased"
  >
    <div
      aria-hidden="true"
      class="pointer-events-none absolute inset-0 -z-10 login-halos"
    />
    <main class="flex min-h-dvh items-center justify-center px-6 py-16">
      <section class="w-full max-w-sm" aria-labelledby="login-title">
        <div class="mb-8 flex items-center justify-center gap-2.5">
          <img
            src="/favicon.webp"
            alt=""
            width="32"
            height="32"
            class="h-8 w-8 object-contain"
          />
          <span class="text-lg font-semibold tracking-tight text-zinc-900"
            >Galadril</span
          >
        </div>
        <div
          class="rounded-2xl border border-white bg-white/85 p-6 shadow-[0_8px_40px_-16px_rgba(120,80,20,0.14)] ring-1 ring-zinc-950/5 sm:p-8"
        >
          <Transition name="login-step" mode="out-in" @after-enter="focusStep">
            <div
              :key="mfa ? 'passkey' : localForm ? 'password' : 'email'"
              ref="stepContent"
            >
              <div class="mb-7 space-y-2 text-center">
                <h1
                  id="login-title"
                  class="text-2xl font-semibold tracking-tight text-zinc-950"
                >
                  {{
                    mfa
                      ? passkeyStep?.operation === "registration"
                        ? "Create your passkey"
                        : "Verify it’s you"
                      : "Sign in to Galadril"
                  }}
                </h1>
                <p class="text-sm leading-relaxed text-zinc-500">
                  {{
                    mfa
                      ? passkeyStep?.operation === "registration"
                        ? "Your organization requires a passkey. Create one on your device or security key to continue."
                        : "Use your passkey to finish signing in."
                      : localForm
                        ? "Enter your password to continue."
                        : "Your work email. Your organization’s sign-in."
                  }}
                </p>
              </div>
              <p
                v-if="
                  route.query.expired === '1' || route.query.recovery === '1'
                "
                role="status"
                class="mb-6 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900"
              >
                {{
                  route.query.recovery === "1"
                    ? "Your administrator reset your passkey. Sign in again to create a new one."
                    : "Your session expired after inactivity. Sign in again to continue."
                }}
              </p>
              <p
                v-if="loginError"
                role="alert"
                class="mb-6 rounded-lg border border-red-200 bg-red-50 p-3 text-xs leading-relaxed text-red-700"
              >
                {{ loginError }}
              </p>
              <form
                v-if="mfa"
                class="space-y-5"
                :aria-busy="submitting"
                @submit.prevent="submitPasskey"
              >
                <p
                  class="rounded-lg bg-stone-50 p-4 text-sm leading-relaxed text-zinc-600"
                >
                  Your device may ask for a fingerprint, face, PIN or security
                  key. Galadril never receives your biometric information.
                </p>
                <UtilsButton
                  type="submit"
                  :disabled="submitting || !passkeyStep"
                  class="w-full gap-2 !py-2.5 focus-visible:ring-2 focus-visible:ring-amber-500 focus-visible:ring-offset-2 disabled:opacity-50"
                  >{{
                    submitting
                      ? "Verifying…"
                      : passkeyStep?.operation === "registration"
                        ? "Create passkey"
                        : "Use passkey"
                  }}<ArrowRightIcon class="h-4 w-4" aria-hidden="true"
                /></UtilsButton>
                <NuxtLink
                  to="/login"
                  class="block text-center text-xs text-zinc-500 hover:text-zinc-800"
                  >Start again</NuxtLink
                >
              </form>
              <form
                v-else-if="localForm"
                :action="localForm.action"
                method="post"
                class="space-y-5"
                :aria-busy="submitting"
                @submit.prevent="submitPassword"
              >
                <p
                  v-for="message in localForm.messages"
                  :key="message.text"
                  :role="message.type === 'error' ? 'alert' : 'status'"
                  class="rounded-lg border p-3 text-xs leading-relaxed"
                  :class="
                    message.type === 'error'
                      ? 'border-red-200 bg-red-50 text-red-700'
                      : 'border-amber-200/60 bg-amber-50/60 text-amber-800'
                  "
                >
                  {{ message.text }}
                </p>
                <template
                  v-for="node in localForm.nodes"
                  :key="node.attributes.name"
                >
                  <template v-if="node.type === 'input'">
                    <UtilsButton
                      v-if="node.attributes.type === 'submit'"
                      :name="node.attributes.name"
                      :value="node.attributes.value"
                      type="submit"
                      :disabled="node.attributes.disabled"
                      class="w-full gap-2 !py-2.5 focus-visible:ring-2 focus-visible:ring-amber-500 focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      Sign in
                      <ArrowRightIcon class="h-4 w-4" aria-hidden="true" />
                    </UtilsButton>
                    <div
                      v-else-if="
                        ['text', 'password', 'email', 'hidden'].includes(
                          node.attributes.type,
                        )
                      "
                      :class="
                        node.attributes.type === 'hidden'
                          ? 'hidden'
                          : 'space-y-2'
                      "
                    >
                      <label
                        v-if="node.attributes.type !== 'hidden'"
                        :for="`local-${node.attributes.name}`"
                        class="block text-xs font-semibold text-zinc-700"
                      >
                        {{
                          node.attributes.name === "identifier"
                            ? "Work email"
                            : (node.meta.label?.text ?? node.attributes.name)
                        }}
                      </label>
                      <input
                        :id="`local-${node.attributes.name}`"
                        :name="node.attributes.name"
                        :type="node.attributes.type"
                        :value="node.attributes.value"
                        :required="node.attributes.required"
                        :autocomplete="
                          node.attributes.type === 'password'
                            ? 'current-password'
                            : 'username'
                        "
                        :disabled="node.attributes.disabled"
                        class="w-full rounded-lg border border-zinc-200 bg-stone-50 px-3 py-2.5 text-sm text-zinc-900 outline-none transition-colors placeholder:text-zinc-400 focus:border-amber-500 focus:ring-2 focus:ring-amber-500/15"
                      />
                    </div>
                  </template>
                </template>
              </form>
              <form
                v-else
                action="/auth/start"
                method="post"
                class="space-y-5"
                :aria-busy="submitting"
                @submit.prevent="submitEmail"
              >
                <div class="space-y-2">
                  <label
                    for="work-email"
                    class="block text-xs font-semibold text-zinc-700"
                    >Work email</label
                  >
                  <input
                    id="work-email"
                    name="email"
                    type="email"
                    autocomplete="username"
                    placeholder="you@company.com"
                    required
                    maxlength="254"
                    class="w-full rounded-lg border border-zinc-200 bg-stone-50 px-3 py-2.5 text-sm text-zinc-900 outline-none transition-colors placeholder:text-zinc-400 focus:border-amber-500 focus:ring-2 focus:ring-amber-500/15"
                  />
                </div>
                <UtilsButton
                  type="submit"
                  :disabled="submitting"
                  class="w-full gap-2 !py-2.5 focus-visible:ring-2 focus-visible:ring-amber-500 focus-visible:ring-offset-2 disabled:cursor-wait disabled:opacity-50"
                >
                  {{ submitting ? "Connecting…" : "Continue" }}
                  <ArrowRightIcon class="h-4 w-4" aria-hidden="true" />
                </UtilsButton>
              </form>
            </div>
          </Transition>
          <div
            class="mt-6 flex items-start gap-2 border-t border-zinc-100 pt-5"
          >
            <LockClosedIcon
              class="mt-0.5 h-3.5 w-3.5 shrink-0 text-zinc-400"
              aria-hidden="true"
            />
            <p class="text-xs leading-relaxed text-zinc-500">
              Your organization manages your credentials and access.
            </p>
          </div>
        </div>
      </section>
    </main>
  </div>
</template>

<style scoped>
.login-halos {
  background:
    radial-gradient(
      ellipse 22rem 19rem at 18% 24%,
      rgb(251 191 36 / 0.13),
      transparent 72%
    ),
    radial-gradient(
      ellipse 20rem 23rem at 87% 68%,
      rgb(245 158 11 / 0.11),
      transparent 72%
    ),
    radial-gradient(
      ellipse 15rem 13rem at 47% 94%,
      rgb(253 230 138 / 0.14),
      transparent 75%
    );
}
.login-step-enter-active,
.login-step-leave-active {
  transition:
    opacity 180ms ease,
    transform 180ms cubic-bezier(0.2, 0.8, 0.2, 1);
}
.login-step-enter-from {
  opacity: 0;
  transform: translateX(14px);
}
.login-step-leave-to {
  opacity: 0;
  transform: translateX(-14px);
}
@media (prefers-reduced-motion: reduce) {
  .login-step-enter-active,
  .login-step-leave-active {
    transition: none;
  }
  .login-step-enter-from,
  .login-step-leave-to {
    transform: none;
  }
}
</style>
