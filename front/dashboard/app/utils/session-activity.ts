/** Background or idle tabs must not keep a shared browser session alive indefinitely. */
import { record } from "#shared/records.ts";

interface SessionActivity {
  hidden: boolean;
  modalOpen: boolean;
  path: string;
  lastActivity: number;
  now: number;
  expiresAt?: number;
  lastCheck?: number;
}
export function shouldRefreshSession({
  hidden,
  modalOpen,
  path,
  lastActivity,
  now,
  expiresAt,
  lastCheck,
}: SessionActivity) {
  return (
    !hidden &&
    !modalOpen &&
    path !== "/login" &&
    Number.isFinite(lastActivity) &&
    Number.isFinite(now) &&
    now >= lastActivity &&
    now - lastActivity <= 5 * 60 * 1000 &&
    (!Number.isFinite(lastCheck) ||
      (lastCheck !== undefined &&
        now - lastCheck >= 30_000 &&
        (now - lastCheck >= 5 * 60 * 1000 ||
          (expiresAt !== undefined &&
            Number.isFinite(expiresAt) &&
            expiresAt * 1000 - now <= 120_000))))
  );
}

/** Transport failures preserve the session; only explicit identity failures require sign-in. */
export function sessionFailureAction(value: unknown) {
  const error = record(value);
  const status = error.statusCode ?? record(error.response).status;
  if (status === 401 || error?.code === "REAUTHENTICATION_REQUIRED")
    return "reauthenticate";
  if (status === 428) return "passkey";
  return "retry";
}

/** A valid sealed renewal can outlive its access cookie while someone reads a page. */
export async function navigationSession<T>(
  request: (
    path: "/auth/session" | "/auth/refresh",
    options: { credentials: "same-origin"; method?: "POST" },
  ) => Promise<T>,
) {
  const options = { credentials: "same-origin" as const };
  try {
    return await request("/auth/session", options);
  } catch (error) {
    if (sessionFailureAction(error) !== "reauthenticate") throw error;
  }
  await request("/auth/refresh", { ...options, method: "POST" });
  return request("/auth/session", options);
}
