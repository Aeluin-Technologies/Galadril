import { authConfig } from "../../auth/config.ts";

export default defineEventHandler(() => {
  const config = authConfig();
  if (!config.mcpClients?.length) throw createError({ statusCode: 404 });
  return {
    issuer: config.issuer,
    authorization_endpoint: `${config.origin}/auth/mcp/authorize`,
    token_endpoint: `${config.origin}/auth/mcp/token`,
    jwks_uri: `${config.origin}/.well-known/jwks.json`,
    response_types_supported: ["code"],
    grant_types_supported: ["authorization_code", "refresh_token"],
    token_endpoint_auth_methods_supported: ["none"],
    code_challenge_methods_supported: ["S256"],
    scopes_supported: [
      ...new Set(
        config.mcpClients.flatMap((client) => [
          ...client.scopes,
          "openid",
          "offline_access",
        ]),
      ),
    ],
  };
});
