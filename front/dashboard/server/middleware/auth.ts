import { authConfig } from "../auth/config.mjs";
import { verifyAccessToken } from "../auth/tokens.mjs";
import { activeUser } from "../auth/directory.mjs";
import { isPublicRoute, isDashboardDocument } from "../auth/policy.mjs";
import { SecondFactorRequiredError } from "../auth/passkey.mjs";
import { AuthenticationUpstreamError } from "../auth/http.mjs";
import { errors as jwtErrors } from "jose";
import { assertActiveSession, sessionLifetime } from "../auth/renewal.mjs";

export default defineEventHandler(async (event) => {
  const path = getRequestURL(event).pathname;
  if (isPublicRoute(path)) return;
  setResponseHeader(event, "Cache-Control", "no-store");
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
