import {
  startAuthentication,
  startRegistration,
} from "@simplewebauthn/browser";
import {
  beginStepUp,
  completePasskeySignIn,
  passkeyFailure,
  requestPasskey,
} from "~/utils/auth-navigation.ts";
import { sessionFailureAction } from "~/utils/session-activity.ts";

type PasskeyStep =
  | {
      operation: "registration";
      options: Parameters<typeof startRegistration>[0]["optionsJSON"];
    }
  | {
      operation: "authentication";
      options: Parameters<typeof startAuthentication>[0]["optionsJSON"];
    };
let pending: Promise<void> | undefined;
let resolvePending: (() => void) | undefined;

export function useSessionState() {
  const connectionError = useState("session-connection-error", () => "");
  const open = useState("session-passkey-open", () => false);
  const busy = useState("session-passkey-busy", () => false);
  const error = useState<string | undefined>(
    "session-passkey-error",
    () => undefined,
  );
  const step = useState<PasskeyStep | undefined>(
    "session-passkey-step",
    () => undefined,
  );

  async function prepare() {
    busy.value = true;
    error.value = undefined;
    try {
      await beginStepUp(window.location.origin);
      step.value = await $fetch<PasskeyStep>("/auth/mfa/options");
    } catch (failure: unknown) {
      if (sessionFailureAction(failure) === "reauthenticate") {
        const code =
          (failure as { code?: string }).code ||
          (failure as { data?: { data?: { code?: string } } }).data?.data?.code;
        open.value = false;
        resolvePending?.();
        pending = undefined;
        resolvePending = undefined;
        await navigateTo(
          code === "REAUTHENTICATION_REQUIRED"
            ? "/login?recovery=1"
            : "/login?expired=1",
        );
        return;
      }
      error.value =
        "Your passkey could not be prepared. Retry to continue. Your organization's passkey requirement is already active.";
    } finally {
      busy.value = false;
    }
  }

  function requirePasskey() {
    if (!pending) {
      open.value = true;
      pending = new Promise<void>((resolve) => {
        resolvePending = resolve;
      });
      void prepare();
    }
    return pending;
  }

  async function verify() {
    if (!step.value || busy.value) return;
    busy.value = true;
    error.value = undefined;
    try {
      const proof = await requestPasskey(step.value, {
        startAuthentication,
        startRegistration,
      });
      const path = await completePasskeySignIn(proof, window.location.origin);
      if (path !== "/") throw new Error("Unexpected passkey result");
      step.value = undefined;
      open.value = false;
      resolvePending?.();
      resolvePending = undefined;
      pending = undefined;
    } catch (failure) {
      error.value = passkeyFailure(failure).message;
      step.value = undefined;
    } finally {
      busy.value = false;
    }
  }
  return {
    open,
    busy,
    error,
    step,
    connectionError,
    requirePasskey,
    prepare,
    verify,
  };
}
