import { timingSafeEqual } from "node:crypto";
import { record } from "#shared/records.ts";
import type { AccessClaims } from "./contracts.ts";

const translationAsset =
  /^\/_i18n\/[A-Za-z0-9_-]{1,128}\/[A-Za-z0-9_-]{1,64}\/messages\.json$/;

/** Renewal may expose only an empty client-rendered shell, never an API or SSR payload. */
export function isDashboardDocument(path: string) {
  const canonical = path.replace(/\/$/, "") || "/";
  if (/\/_payload\.(?:json|js)$/.test(canonical)) return false;
  return (
    [
      "/",
      "/builder",
      "/pipeline/new",
      "/settings/sso",
      "/settings/email",
      "/pengolo",
    ].includes(canonical) || /^\/pengolo\/[^/]{1,256}$/.test(canonical)
  );
}

/** Locale messages must load on the anonymous login page and remain cached after sign-in. */
export function isPublicRoute(path: string) {
  return (
    path === "/login" ||
    path.startsWith("/auth/") ||
    path.startsWith("/_nuxt/") ||
    path.startsWith("/__nuxt") ||
    path.startsWith("/favicon") ||
    path === "/healthz" ||
    translationAsset.test(path)
  );
}

export function emailDomain(email: unknown) {
  if (
    typeof email !== "string" ||
    email.length > 254 ||
    !/^[^\s@]+@[a-z0-9.-]+$/i.test(email)
  )
    throw new Error("Invalid email");
  return email.slice(email.indexOf("@") + 1).toLowerCase();
}

export function discoverConnection<T extends { domain: string }>(
  email: unknown,
  connections: T[],
) {
  const domain = emailDomain(email);
  const matches = connections.filter((item) => item.domain === domain);
  const connection = matches[0];
  if (matches.length !== 1 || !connection)
    throw new Error("Unavailable connection");
  return connection;
}

export function requireOrigin(actual: unknown, expected: string) {
  if (actual !== expected) throw new Error("Invalid origin");
}

export function constantEqual(expected: unknown, actual: unknown) {
  if (
    typeof expected !== "string" ||
    typeof actual !== "string" ||
    !expected.length ||
    actual.length > 2048
  )
    return false;
  const left = Buffer.from(expected);
  const right = Buffer.from(actual);
  return left.length === right.length && timingSafeEqual(left, right);
}

export function validateConsent(value: unknown) {
  const request = record(value);
  if (
    record(request.client).client_id !== "galadril-studio" ||
    !Array.isArray(request.requested_scope) ||
    !request.requested_scope.includes("openid") ||
    request.requested_scope.length > 2 ||
    new Set(request.requested_scope).size !== request.requested_scope.length ||
    request.requested_scope.some(
      (scope) => !["openid", "offline_access"].includes(scope),
    ) ||
    !Array.isArray(request.requested_access_token_audience) ||
    request.requested_access_token_audience.length !== 1 ||
    request.requested_access_token_audience[0] !== "galadril"
  )
    throw new Error("Invalid consent");
  return { scope: [...request.requested_scope], audience: ["galadril"] };
}

export function accessClaims(value: unknown): AccessClaims {
  const payload = record(value);
  if (
    typeof payload.sub !== "string" ||
    !payload.sub.length ||
    payload.sub.length > 256 ||
    typeof payload.tenant_id !== "string" ||
    !/^[a-zA-Z0-9_-]{1,64}$/.test(payload.tenant_id) ||
    payload.token_use !== "access" ||
    typeof payload.iat !== "number" ||
    !Number.isSafeInteger(payload.iat) ||
    typeof payload.exp !== "number" ||
    !Number.isSafeInteger(payload.exp) ||
    payload.iat >= payload.exp
  )
    throw new Error("Invalid access claims");
  return {
    ...payload,
    sub: payload.sub,
    tenant_id: payload.tenant_id,
    token_use: "access",
    iat: payload.iat,
    exp: payload.exp,
  };
}

export function externalSubject(value: unknown, connection: string) {
  const profile = record(value);
  if (
    record(profile.requested).client_id !== connection ||
    typeof profile.id !== "string" ||
    !profile.id.length ||
    profile.id.length > 1024
  )
    throw new Error("Invalid external identity");
  return profile.id;
}

export function safeRedirect(value: unknown, origin: string) {
  if (typeof value !== "string") throw new Error("Invalid redirect");
  const url = new URL(value);
  if (url.origin !== origin || url.username || url.password)
    throw new Error("Invalid redirect");
  return url.href;
}
