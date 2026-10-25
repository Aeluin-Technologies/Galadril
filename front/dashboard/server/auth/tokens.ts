import { createRemoteJWKSet, jwtVerify, customFetch } from "jose";
import type { JWTVerifyGetKey } from "jose";
import { requestJson } from "./http.ts";
import { accessClaims } from "./policy.ts";

/** Cache only issuer keys; TLS and the bounded transport remain mandatory during renewal. */
export function trustedKeys(url: string) {
  const endpoint = new URL(url);
  if (endpoint.protocol !== "https:") throw new Error("JWKS requires TLS");
  return createRemoteJWKSet(endpoint, {
    [customFetch]: async (input, options) =>
      Response.json(await requestJson(new URL(input), options)),
    timeoutDuration: 3000,
    cooldownDuration: 30000,
    cacheMaxAge: 300000,
  });
}

/** Signature verification cannot replace application claim validation or live IAM checks. */
export async function verifyAccessToken(
  token: unknown,
  issuer: string,
  keys: JWTVerifyGetKey,
) {
  if (typeof token !== "string") throw new Error("Missing token");
  const { payload } = await jwtVerify(token, keys, {
    issuer,
    audience: "galadril",
    algorithms: ["RS256"],
    requiredClaims: ["sub", "iat", "exp", "tenant_id", "token_use"],
    clockTolerance: 0,
  });
  return accessClaims(payload);
}
