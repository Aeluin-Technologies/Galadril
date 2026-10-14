/** Visible, recently active tabs keep the session alive; background tabs never extend it. */
import {
  shouldRefreshSession,
  sessionFailureAction,
} from "~/utils/session-activity.mjs";

export default defineNuxtPlugin((nuxt) => {
  let lastActivity = Date.now();
  let running = false;
  const route = useRouter();
  const { requirePasskey, open, connectionError } = useSessionState();
  const activity = () => {
    lastActivity = Date.now();
  };
  const events = ["pointerdown", "keydown", "scroll", "touchstart"] as const;
  for (const event of events)
    window.addEventListener(event, activity, { passive: true });
  async function heartbeat() {
    if (
      running ||
      !shouldRefreshSession({
        modalOpen: open.value,
        hidden: document.hidden,
        lastActivity,
        now: Date.now(),
        path: route.currentRoute.value.path,
      })
    )
      return;
    running = true;
    const renew = async () => {
      try {
        await $fetch("/auth/refresh", {
          method: "POST",
          credentials: "same-origin",
        });
        connectionError.value = "";
      } catch (error: unknown) {
        const action = sessionFailureAction(error);
        if (action === "passkey") await requirePasskey();
        else if (action === "reauthenticate")
          await navigateTo("/login?expired=1");
        else
          connectionError.value =
            "Your session could not be checked. You are still signed in; we’ll retry while you are active.";
      }
    };
    try {
      if (navigator.locks)
        await navigator.locks.request("galadril-session-renewal", renew);
      else await renew();
    } catch {
      connectionError.value =
        "Your session could not be checked. You are still signed in; we’ll retry while you are active.";
    } finally {
      running = false;
    }
  }
  const timer = window.setInterval(() => {
    void heartbeat();
  }, 60_000);
  const visible = () => {
    if (!document.hidden) {
      activity();
      void heartbeat();
    }
  };
  document.addEventListener("visibilitychange", visible);
  nuxt.vueApp.onUnmount( () => {
    window.clearInterval(timer);
    document.removeEventListener("visibilitychange", visible);
    for (const event of events) window.removeEventListener(event, activity);
  });
});
