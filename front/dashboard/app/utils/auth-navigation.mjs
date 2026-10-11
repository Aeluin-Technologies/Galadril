/** Credential-bearing requests and SPA navigation must stay on the trusted origin. */
function trustedTarget(value, origin, paths) {
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

function navigationPath(response, origin) {
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

const requestOptions = () => ({
  credentials: "same-origin",
  mode: "same-origin",
  redirect: "follow",
  signal: AbortSignal.timeout(15000),
});

/** Enterprise IdPs need top-level navigation; local Ory flows can remain in the SPA. */
export async function beginSignIn(email, origin, request = fetch) {
  const response = await request(new URL("/auth/start", origin).href, {
    ...requestOptions(),
    method: "POST",
    headers: { accept: "application/json" },
    body: new URLSearchParams({ email }),
  });
  if (!response.ok) throw new Error("Authentication unavailable");
  const result = await response.json();
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
  action,
  fields,
  origin,
  request = fetch,
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
    return `/login?flow=${encodeURIComponent(target.searchParams.get("flow"))}`;
  }
  return navigationPath(response, origin);
}

/** Continues the same browser flow only after the server verifies the authenticator. */
export async function completePasskeySignIn(proof, origin, request = fetch) {
  const response = await request(new URL("/auth/mfa/verify", origin).href, {
    ...requestOptions(),
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(proof),
  });
  return navigationPath(response, origin);
}

/** Invoke within the click handler so browsers retain transient user activation. */
export async function requestPasskey(step, authenticators) {
  if (step?.operation === "registration")
    return authenticators.startRegistration({ optionsJSON: step.options });
  if (step?.operation === "authentication")
    return authenticators.startAuthentication({ optionsJSON: step.options });
  throw new Error("Missing passkey challenge");
}

/** Browser errors are intentionally ambiguous; never infer that a key was lost. */
export function passkeyFailure(error) {
  switch (error?.name) {
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
