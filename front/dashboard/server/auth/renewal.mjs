export const sessionLifetime = 8 * 60 * 60;
export const sessionIdleTimeout = 30 * 60;

export function shouldRenew(expiresAt, now = Math.floor(Date.now() / 1000)) {
  return !Number.isSafeInteger(expiresAt) || expiresAt <= now + 120;
}

/** Missing refresh secrets must fail before publishing a partially usable session. */
export function requireRefreshToken(result) {
  if (typeof result?.refresh_token !== "string" || !result.refresh_token)
    throw new Error("Missing refresh token");
  return result.refresh_token;
}

/** A remote revocation outage must never retain credentials on this browser. */
export async function logoutSession(refreshToken, services) {
  try {
    if (refreshToken) await services.revoke(refreshToken);
  } finally {
    await services.clear();
  }
}

/** Refresh must retain the original identity and enforce current revocation and MFA state. */
export function assertActiveSession(state, now = Date.now()) {
  if (
    !state?.refreshToken ||
    !Number.isFinite(state.createdAt) ||
    !Number.isFinite(state.lastActiveAt) ||
    state.createdAt > now ||
    state.lastActiveAt > now ||
    now - state.createdAt >= sessionLifetime * 1000 ||
    now - state.lastActiveAt >= sessionIdleTimeout * 1000
  )
    throw new Error("Session expired");
}

export async function refreshSession(state, services, now = Date.now()) {
  assertActiveSession(state, now);
  const result = await services.exchange(state.refreshToken);
  const refreshToken = requireRefreshToken(result);
  const claims = await services.verify(result.access_token);
  if (claims.sub !== state.subject || claims.tenant_id !== state.tenant)
    throw new Error("Session identity changed");
  await services.active(claims.tenant_id, claims.sub);
  await services.check(claims);
  return {
    ...state,
    refreshToken,
    accessToken: result.access_token,
    expiresAt: claims.exp,
    lastActiveAt: now,
  };
}
