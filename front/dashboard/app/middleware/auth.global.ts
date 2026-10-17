import { beginStepUp } from "~/utils/auth-navigation.mjs";
import type { AccountSession } from "~/composables/useAccount";
import {
  navigationSession,
  sessionFailureAction,
} from "~/utils/session-activity.mjs";

export default defineNuxtRouteMiddleware(async (to) => {
  if (to.path === "/login") return;
  const { connectionError } = useSessionState();
  try {
    let session: AccountSession | { requiresPasskey: true };
    try {
      const check = () => navigationSession($fetch);
      session =
        import.meta.client && navigator.locks
          ? await navigator.locks.request("galadril-session-renewal", check)
          : await check();
    } catch (error) {
      if (sessionFailureAction(error) !== "passkey") throw error;
      session = { requiresPasskey: true };
    }
    if ("subject" in session)
      useState<AccountSession | null>("account-session", () => null).value =
        session;
    if (session.requiresPasskey && import.meta.client) {
      // The default layout cannot show a modal until the first route has mounted.
      if (useNuxtApp().isHydrating) {
        await beginStepUp(window.location.origin);
        return navigateTo("/login?mfa=1");
      }
      await useSessionState().requirePasskey();
    }
    connectionError.value = "";
  } catch (error: unknown) {
    if (sessionFailureAction(error) === "reauthenticate")
      return navigateTo("/login?expired=1");
    connectionError.value =
      "Your session could not be checked. Check your connection and try this page again.";
    if (useNuxtApp().isHydrating)
      throw createError({
        statusCode: 503,
        statusMessage:
          "Your session could not be checked. Please try again shortly.",
      });
    return abortNavigation();
  }
});
