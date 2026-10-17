import { createRemoteJWKSet, jwtVerify, customFetch } from "jose";
import { requestJson } from "./http.mjs";
import { accessClaims } from "./policy.mjs";

export function trustedKeys(url) {
  const endpoint = new URL(url);
  if (endpoint.protocol !== "https:") throw new Error("JWKS requires TLS");
  return createRemoteJWKSet(endpoint, {
    [customFetch]: async (input, options) => Response.json(await requestJson(new URL(input), options)),
    timeoutDuration: 3000,
    cooldownDuration: 30000,
    cacheMaxAge: 300000,
  });
}

export async function verifyAccessToken(token, issuer, keys) {
  const { payload } = await jwtVerify(token, keys, {
    issuer,
    audience: "galadril",
    algorithms: ["RS256"],
    requiredClaims: ["sub", "iat", "exp", "tenant_id", "token_use"],
    clockTolerance: 0,
  });
  return accessClaims(payload);
}
