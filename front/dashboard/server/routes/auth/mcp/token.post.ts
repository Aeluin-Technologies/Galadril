import { jwtVerify } from "jose";
import { authConfig } from "../../../auth/config.ts";
import { requestObject } from "../../../auth/http.ts";
import { record, text } from "#shared/records.ts";

export default defineEventHandler(async (event) => {
  setResponseHeader(event, "Cache-Control", "no-store");
  setResponseHeader(event, "Pragma", "no-cache");
  const config = authConfig();
  const body = await readRawBody(event);
  if (!body || body.length > 8192) throw createError({ statusCode: 400 });
  const parameters = new URLSearchParams(body);
  const client = config.mcpClients?.find(
    (item) => item.id === parameters.get("client_id"),
  );
  if (
    !client ||
    parameters.get("resource") !== client.resource ||
    !["authorization_code", "refresh_token"].includes(
      parameters.get("grant_type") ?? "",
    ) ||
    [...parameters.keys()].some((key) => parameters.getAll(key).length !== 1) ||
    parameters.has("client_secret")
  )
    throw createError({ statusCode: 400 });
  if (
    parameters.get("grant_type") === "authorization_code" &&
    (!client.redirectUris.includes(parameters.get("redirect_uri") ?? "") ||
      !/^[a-zA-Z0-9._~-]{43,128}$/.test(parameters.get("code_verifier") ?? ""))
  )
    throw createError({ statusCode: 400 });
  const forwarded = new URLSearchParams();
  for (const field of [
    "grant_type",
    "client_id",
    "code",
    "redirect_uri",
    "code_verifier",
    "refresh_token",
  ])
    if (parameters.has(field)) forwarded.set(field, parameters.get(field)!);
  const result = await requestObject(new URL("/oauth2/token", config.hydra), {
    method: "POST",
    headers: {
      "content-type": "application/x-www-form-urlencoded",
      "x-forwarded-proto": "https",
    },
    body: forwarded.toString(),
  });
  const { payload } = await jwtVerify(text(result.access_token), config.keys, {
    issuer: config.issuer,
    audience: client.resource,
    algorithms: ["RS256"],
    requiredClaims: ["sub", "exp", "iat"],
  });
  const claims = record(payload);
  const scopes = Array.isArray(claims.scp)
    ? claims.scp
    : typeof claims.scope === "string"
      ? claims.scope.split(" ")
      : [];
  if (
    (claims.client_id ?? claims.azp) !== client.id ||
    !scopes.some((scope: unknown) => client.scopes.includes(String(scope)))
  )
    throw createError({ statusCode: 401 });
  return result;
});
