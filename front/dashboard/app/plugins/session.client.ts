/** Visible, recently active tabs keep the session alive; background tabs never extend it. */
import {
  shouldRefreshSession,
  sessionFailureAction,
} from "~/utils/session-activity.ts";

export default defineNuxtPlugin((nuxt) => {
  let lastActivity = Date.now();
  let running = false;
  let lastCheck = Date.now();
  const account = useState<{
    subject: string;
    tenant: string;
    expiresAt?: number;
  } | null>("account-session");
  const checkpointKey = "galadril-session-check";
  function synchronize() {
    try {
      const checkpoint = JSON.parse(
        localStorage.getItem(checkpointKey) ?? "null",
      ) as {
        subject: string;
        tenant: string;
        expiresAt: number;
        checkedAt: number;
      } | null;
      if (
        checkpoint &&
        account.value &&
        checkpoint.subject === account.value.subject &&
        checkpoint.tenant === account.value.tenant &&
        Number.isFinite(checkpoint.checkedAt) &&
        checkpoint.checkedAt > lastCheck &&
        checkpoint.checkedAt <= Date.now()
      ) {
        lastCheck = checkpoint.checkedAt;
        if (
          Number.isFinite(checkpoint.expiresAt) &&
          account.value.expiresAt !== checkpoint.expiresAt
        ) {
          account.value.expiresAt = checkpoint.expiresAt;
          window.dispatchEvent(new Event("galadril-session-refreshed"));
        }
      }
    } catch {
      /* A missing or disabled browser store never authorizes a session. */
    }
  }
  const route = useRouter();
  const { requirePasskey, open, connectionError } = useSessionState();
  const activity = () => {
    lastActivity = Date.now();
  };
  const events = ["pointerdown", "keydown", "scroll", "touchstart"] as const;
  for (const event of events)
    window.addEventListener(event, activity, { passive: true });
  async function heartbeat() {
    synchronize();
    if (
      running ||
      !shouldRefreshSession({
        modalOpen: open.value,
        hidden: document.hidden,
        lastActivity,
        now: Date.now(),
        path: route.currentRoute.value.path,
        expiresAt: account.value?.expiresAt,
        lastCheck,
      })
    )
      return;
    running = true;
    const renew = async () => {
      synchronize();
      if (
        !shouldRefreshSession({
          modalOpen: open.value,
          hidden: document.hidden,
          lastActivity,
          now: Date.now(),
          path: route.currentRoute.value.path,
          expiresAt: account.value?.expiresAt,
          lastCheck,
        })
      )
        return;
      try {
        const result = await $fetch<{ expiresAt: number }>("/auth/refresh", {
          method: "POST",
          credentials: "same-origin",
        });
        lastCheck = Date.now();
        if (account.value) {
          const changed = account.value.expiresAt !== result.expiresAt;
          account.value.expiresAt = result.expiresAt;
          try {
            localStorage.setItem(
              checkpointKey,
              JSON.stringify({
                subject: account.value.subject,
                tenant: account.value.tenant,
                expiresAt: result.expiresAt,
                checkedAt: lastCheck,
              }),
            );
          } catch {
            /* Renewal still succeeds when browser storage is unavailable. */
          }
          if (changed)
            window.dispatchEvent(new Event("galadril-session-refreshed"));
        }
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
  }, 30_000);
  const visible = () => {
    if (!document.hidden) {
      activity();
      void heartbeat();
    }
  };
  document.addEventListener("visibilitychange", visible);
  nuxt.vueApp.onUnmount(() => {
    window.clearInterval(timer);
    document.removeEventListener("visibilitychange", visible);
    for (const event of events) window.removeEventListener(event, activity);
  });
});
