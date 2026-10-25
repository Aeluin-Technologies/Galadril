/** Credential-bearing requests and SPA navigation must stay on the trusted origin. */
import { record } from "../../shared/records.ts";

interface NavigationResponse {
  ok: boolean;
  status?: number;
  url?: string;
  json?: () => Promise<unknown>;
}
export type BrowserRequest = (
  url: string,
  options: RequestInit,
) => Promise<NavigationResponse>;

function trustedTarget(value: unknown, origin: string, paths: string[]) {
  if (typeof value !== "string")
    throw new Error("Invalid authentication destination");
  const url = new URL(value, origin);
  if (
    url.origin !== origin ||
    url.username ||
    url.password ||
    !paths.includes(url.pathname)
  )
    throw new Error("Invalid authentication destination");
  return url;
}

function navigationPath(response: NavigationResponse, origin: string) {
  if (!response.ok) throw new Error("Authentication unavailable");
  const url = trustedTarget(response.url, origin, ["/login", "/"]);
  if (
    url.pathname === "/login" &&
    !url.searchParams.get("flow") &&
    url.searchParams.get("mfa") !== "1"
  )
    throw new Error("Missing identity flow");
  return url.pathname + url.search;
}

const requestOptions = (): RequestInit => ({
  credentials: "same-origin",
  mode: "same-origin",
  redirect: "follow",
  signal: AbortSignal.timeout(15000),
});

class ReauthenticationNavigationError extends Error {
  readonly code: string;
  readonly statusCode = 401;
  constructor(code: string) {
    super("Sign in again to confirm your identity");
    this.code = code;
  }
}

/** A signed active session can step up in-place without replaying primary credentials. */
export async function beginStepUp(
  origin: string,
  request: BrowserRequest = fetch,
) {
  const response = await request(new URL("/auth/step-up", origin).href, {
    ...requestOptions(),
    method: "POST",
    headers: { accept: "application/json" },
  });
  if (!response.ok) {
    if (response.status === 401) {
      const problem = record(await response.json?.().catch(() => null));
      throw new ReauthenticationNavigationError(
        record(problem.data).code === "REAUTHENTICATION_REQUIRED"
          ? "REAUTHENTICATION_REQUIRED"
          : "SESSION_UNAVAILABLE",
      );
    }
    throw new Error("Passkey setup unavailable");
  }
  const result = record(await response.json?.());
  const target = trustedTarget(result.redirect, origin, ["/oauth2/auth"]);
  const path = navigationPath(
    await request(target.href, requestOptions()),
    origin,
  );
  if (path !== "/login?mfa=1")
    throw new Error("Invalid passkey setup destination");
}

/** Enterprise IdPs need top-level navigation; local Ory flows can remain in the SPA. */
export async function beginSignIn(
  email: string,
  origin: string,
  request: BrowserRequest = fetch,
) {
  const response = await request(new URL("/auth/start", origin).href, {
    ...requestOptions(),
    method: "POST",
    headers: { accept: "application/json" },
    body: new URLSearchParams({ email }),
  });
  if (!response.ok) throw new Error("Authentication unavailable");
  const result = record(await response.json?.());
  if (typeof result.local !== "boolean")
    throw new Error("Invalid authentication provider");
  const target = trustedTarget(result.redirect, origin, ["/oauth2/auth"]);
  if (!result.local)
    return { path: target.pathname + target.search, external: true };
  const flow = await request(target.href, requestOptions());
  return { path: navigationPath(flow, origin), external: false };
}

/** Fetch processes Ory's cookies while native form fields preserve its CSRF contract. */
export async function completePasswordSignIn(
  action: string,
  fields: URLSearchParams | FormData,
  origin: string,
  request: BrowserRequest = fetch,
) {
  const target = trustedTarget(action, origin, [
    "/identity/self-service/login",
  ]);
  if (!target.searchParams.get("flow"))
    throw new Error("Missing identity flow");
  const response = await request(target.href, {
    ...requestOptions(),
    method: "POST",
    body: fields,
  });
  if (response.status === 400) {
    const retry = trustedTarget(response.url, origin, [target.pathname]);
    if (retry.searchParams.get("flow") !== target.searchParams.get("flow"))
      throw new Error("Identity flow changed");
    return `/login?flow=${encodeURIComponent(target.searchParams.get("flow") ?? "")}`;
  }
  return navigationPath(response, origin);
}

/** Continues the same browser flow only after the server verifies the authenticator. */
export async function completePasskeySignIn(
  proof: unknown,
  origin: string,
  request: BrowserRequest = fetch,
) {
  const response = await request(new URL("/auth/mfa/verify", origin).href, {
    ...requestOptions(),
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(proof),
  });
  return navigationPath(response, origin);
}

/** Invoke within the click handler so browsers retain transient user activation. */
export async function requestPasskey<R, A, RR, AR>(
  step:
    | { operation: "registration"; options: R }
    | { operation: "authentication"; options: A }
    | undefined,
  authenticators: {
    startRegistration: (value: { optionsJSON: R }) => Promise<RR>;
    startAuthentication: (value: { optionsJSON: A }) => Promise<AR>;
  },
) {
  if (step?.operation === "registration")
    return authenticators.startRegistration({ optionsJSON: step.options });
  if (step?.operation === "authentication")
    return authenticators.startAuthentication({ optionsJSON: step.options });
  throw new Error("Missing passkey challenge");
}

/** Browser errors are intentionally ambiguous; never infer that a key was lost. */
export function passkeyFailure(error: unknown) {
  switch (record(error).name) {
    case "NotAllowedError":
      return {
        reason: "not_allowed",
        message:
          "The browser could not complete the passkey request. Try again with the device or password manager holding your key. If no prompt appears, open this address in Safari or Chrome.",
      };
    case "SecurityError":
      return {
        reason: "security",
        message:
          "The browser blocked this passkey request. Open the original HTTPS address used to create your key.",
      };
    case "NotSupportedError":
      return {
        reason: "unsupported",
        message:
          "This browser or device cannot use this passkey. Open this address in Safari or Chrome, or use a compatible security key.",
      };
    case "AbortError":
      return {
        reason: "cancelled",
        message: "The passkey request was cancelled. Please try again.",
      };
    case "InvalidStateError":
      return {
        reason: "already_registered",
        message:
          "This key is already registered. Please start again to sign in.",
      };
    default:
      return {
        reason: "failed",
        message:
          "Passkey verification could not be completed. Please try again or start a new sign-in.",
      };
  }
}
