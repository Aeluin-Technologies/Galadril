import { authConfig } from "../auth/config.mjs";
import { verifyAccessToken } from "../auth/tokens.mjs";
import { activeUser } from "../auth/directory.mjs";
import { isPublicRoute } from "../auth/policy.mjs";

export default defineEventHandler(async (event) => {
  const path = getRequestURL(event).pathname;
  if (isPublicRoute(path)) return;
  setResponseHeader(event, "Cache-Control", "no-store");
  try {
    const token = getCookie(event, "__Host-galadril-token");
    if (!token) throw new Error("Missing token");
    const config = authConfig();
    const claims = await verifyAccessToken(token, config.issuer, config.keys);
    await activeUser(config.directory, claims.tenant_id, claims.sub);
    await config.passkeys.check(claims);
  } catch {
    return sendRedirect(event, "/login", 303);
  }
});
