import {
  beginSignIn,
  completePasswordSignIn,
  completePasskeySignIn,
  requestPasskey,
  passkeyFailure,
} from "~/utils/auth-navigation.ts";
import {
  startRegistration,
  startAuthentication,
  type PublicKeyCredentialCreationOptionsJSON,
  type PublicKeyCredentialRequestOptionsJSON,
} from "@simplewebauthn/browser";
import type { IdentityForm } from "~/types/auth";

/** Keeps authentication transitions together so each form retains its native submission contract. */
export function useSignIn() {
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
          loginError.value =
            "This sign-in attempt expired. Please start again.";
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
      if (
        destination === route.fullPath &&
        typeof route.query.flow === "string"
      )
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
  return {
    route,
    submitting,
    mfa,
    passkeyStep,
    submitPasskey,
    localForm,
    loginError,
    stepContent,
    loadLocalForm,
    focusStep,
    submitEmail,
    submitPassword,
  };
}
