import { record, text } from "../../shared/records.ts";
export const sessionLifetime = 8 * 60 * 60;
export const sessionIdleTimeout = 30 * 60;

/** Renew before expiry so an active browser can preserve its document during token rotation. */
export function shouldRenew(
  expiresAt: unknown,
  now = Math.floor(Date.now() / 1000),
) {
  return (
    typeof expiresAt !== "number" ||
    !Number.isSafeInteger(expiresAt) ||
    expiresAt <= now + 120
  );
}

/** Missing refresh secrets must fail before publishing a partially usable session. */
export function requireRefreshToken(value: unknown) {
  const result = record(value);
  if (typeof result?.refresh_token !== "string" || !result.refresh_token)
    throw new Error("Missing refresh token");
  return result.refresh_token;
}

/** A remote revocation outage must never retain credentials on this browser. */
export async function logoutSession(
  refreshToken: unknown,
  services: {
    revoke(token: string): Promise<unknown>;
    prepareIdentityLogout?: () => Promise<unknown>;
    clear(): Promise<unknown>;
  },
) {
  try {
    if (refreshToken) await services.revoke(text(refreshToken));
    await services.prepareIdentityLogout?.();
  } finally {
    await services.clear();
  }
}

/** Refresh must retain the original identity and enforce current revocation and MFA state. */
export function assertActiveSession(
  value: unknown,
  now = Date.now(),
): asserts value is RenewalSession {
  const state = record(value);
  if (
    typeof state.refreshToken !== "string" ||
    !state.refreshToken ||
    typeof state.createdAt !== "number" ||
    !Number.isFinite(state.createdAt) ||
    typeof state.lastActiveAt !== "number" ||
    !Number.isFinite(state.lastActiveAt) ||
    state.createdAt > now ||
    state.lastActiveAt > now ||
    now - state.createdAt >= sessionLifetime * 1000 ||
    now - state.lastActiveAt >= sessionIdleTimeout * 1000
  )
    throw new Error("Session expired");
}

export async function refreshSession(
  state: unknown,
  services: {
    exchange(token: string): Promise<unknown>;
    verify(token: string): Promise<RenewalClaims>;
    active(tenant: string, subject: string): Promise<unknown>;
    check(claims: RenewalClaims): Promise<unknown>;
  },
  now = Date.now(),
) {
  assertActiveSession(state, now);
  const result = record(await services.exchange(state.refreshToken));
  const refreshToken = requireRefreshToken(result);
  const accessToken = text(result.access_token);
  const claims = await services.verify(accessToken);
  if (claims.sub !== state.subject || claims.tenant_id !== state.tenant)
    throw new Error("Session identity changed");
  await services.active(claims.tenant_id, claims.sub);
  await services.check(claims);
  return {
    ...state,
    refreshToken,
    accessToken,
    expiresAt: claims.exp,
    lastActiveAt: now,
  };
}

export interface RenewalSession extends Record<string, unknown> {
  refreshToken: string;
  createdAt: number;
  lastActiveAt: number;
  subject?: unknown;
  tenant?: unknown;
}

interface RenewalClaims extends Record<string, unknown> {
  sub: string;
  tenant_id: string;
  exp: number;
}
