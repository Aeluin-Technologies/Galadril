/** Background or idle tabs must not keep a shared browser session alive indefinitely. */
export function shouldRefreshSession({
  hidden,
  modalOpen,
  path,
  lastActivity,
  now,
  expiresAt,
  lastCheck,
}) {
  return (
    !hidden &&
    !modalOpen &&
    path !== "/login" &&
    Number.isFinite(lastActivity) &&
    Number.isFinite(now) &&
    now >= lastActivity &&
    now - lastActivity <= 5 * 60 * 1000 &&
    (!Number.isFinite(lastCheck) ||
      (now - lastCheck >= 30_000 &&
        (now - lastCheck >= 5 * 60 * 1000 ||
          (Number.isFinite(expiresAt) && expiresAt * 1000 - now <= 120_000))))
  );
}

/** Transport failures preserve the session; only explicit identity failures require sign-in. */
export function sessionFailureAction(error) {
  const status = error?.statusCode ?? error?.response?.status;
  if (status === 401 || error?.code === "REAUTHENTICATION_REQUIRED")
    return "reauthenticate";
  if (status === 428) return "passkey";
  return "retry";
}

/** A valid sealed renewal can outlive its access cookie while someone reads a page. */
export async function navigationSession(request) {
  const options = { credentials: "same-origin" };
  try {
    return await request("/auth/session", options);
  } catch (error) {
    if (sessionFailureAction(error) !== "reauthenticate") throw error;
  }
  await request("/auth/refresh", { ...options, method: "POST" });
  return request("/auth/session", options);
}
