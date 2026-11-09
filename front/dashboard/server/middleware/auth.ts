import { authConfig } from "../auth/config.ts";
import { verifyAccessToken } from "../auth/tokens.ts";
import { activeUser } from "../auth/directory.ts";
import { isPublicRoute, isDashboardDocument } from "../auth/policy.ts";
import { SecondFactorRequiredError } from "../auth/passkey.ts";
import { AuthenticationUpstreamError } from "../auth/http.ts";
import { errors as jwtErrors } from "jose";
import { assertActiveSession, sessionLifetime } from "../auth/renewal.ts";

export default defineEventHandler(async (event) => {
  const request = getRequestURL(event);
  const path = request.pathname;
  if (
    event.method === "GET" &&
    path === "/login" &&
    process.env.AUTH_PUBLIC_ORIGIN
  ) {
    const canonical = new URL(process.env.AUTH_PUBLIC_ORIGIN);
    if (
      canonical.protocol !== "https:" ||
      canonical.username ||
      canonical.password
    )
      throw new Error("Invalid public authentication origin");
    // Compare hosts because the trusted tunnel terminates HTTPS before this hop.
    if (request.host !== canonical.host) {
      setResponseHeader(event, "Cache-Control", "private, no-store, max-age=0");
      return sendRedirect(event, new URL("/login", canonical.origin).href, 303);
    }
  }
  if (isPublicRoute(path)) return;
  setResponseHeader(event, "Cache-Control", "private, no-store, max-age=0");
  setResponseHeader(event, "Vary", "Cookie, Authorization");
  setResponseHeader(event, "Pragma", "no-cache");
  let accessMissing = false;
  try {
    const token = getCookie(event, "__Host-galadril-token");
    if (!token) {
      accessMissing = true;
      throw new Error("Missing token");
    }
    const config = authConfig();
    const claims = await verifyAccessToken(token, config.issuer, config.keys);
    await activeUser(config.directory, claims.tenant_id, claims.sub);
    await config.passkeys.check(claims);
  } catch (error) {
    if (error instanceof AuthenticationUpstreamError)
      throw createError({
        statusCode: 503,
        statusMessage: "Authentication service temporarily unavailable",
        data: { code: "AUTHENTICATION_UNAVAILABLE" },
      });
    if (
      event.method === "GET" &&
      isDashboardDocument(path) &&
      getHeader(event, "accept")?.includes("text/html") &&
      (accessMissing || error instanceof jwtErrors.JWTExpired)
    ) {
      const sealed = getCookie(event, "__Host-galadril-renewal");
      if (sealed) {
        const config = authConfig();
        const restorable = await unsealSession(
          event,
          {
            name: "__Host-galadril-renewal",
            password: config.sessionSecret,
            maxAge: sessionLifetime,
          },
          sealed,
        )
          .then((session) => {
            assertActiveSession(session.data);
            return true;
          })
          .catch(() => false);
        if (restorable) return;
      }
    }
    if (!getHeader(event, "accept")?.includes("text/html")) {
      throw createError({
        statusCode: error instanceof SecondFactorRequiredError ? 428 : 401,
        data: {
          code:
            error instanceof SecondFactorRequiredError
              ? "PASSKEY_REQUIRED"
              : "SESSION_EXPIRED",
        },
      });
    }
    return sendRedirect(event, "/login", 303);
  }
});
