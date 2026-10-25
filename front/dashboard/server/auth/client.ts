import { requestJson } from "./http.ts";
import { record } from "#shared/records.ts";

export function clientDefinition(config: {
  clientSecret: string;
  origin: string;
}) {
  return {
    client_id: "galadril-studio",
    client_secret: config.clientSecret,
    client_name: "Galadril Studio",
    grant_types: ["authorization_code", "refresh_token"],
    response_types: ["code"],
    scope: "openid offline_access",
    audience: ["galadril"],
    redirect_uris: [`${config.origin}/auth/callback`],
    token_endpoint_auth_method: "client_secret_basic",
    access_token_strategy: "jwt",
    skip_consent: false,
  };
}

let initialization: Promise<void> | undefined;

/** Reconcile the first-party client using the private administrative API. */
export function ensureClient(config: {
  clientSecret: string;
  origin: string;
  admin: string;
}) {
  if (initialization) return initialization;
  initialization = (async () => {
    const definition = clientDefinition(config);
    const url = new URL("/admin/clients/galadril-studio", config.admin);
    const response = await fetch(url, {
      redirect: "error",
      signal: AbortSignal.timeout(5000),
    });
    if (response.status === 404) {
      await requestJson(new URL("/admin/clients", config.admin), {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(definition),
      });
    } else {
      if (!response.ok) throw new Error("Client initialization unavailable");
      const client = record(await response.json());
      const changed = [];
      for (const name of [
        "redirect_uris",
        "grant_types",
        "response_types",
        "audience",
        "scope",
        "token_endpoint_auth_method",
        "access_token_strategy",
        "skip_consent",
      ] satisfies (keyof typeof definition)[]) {
        if (JSON.stringify(client[name]) !== JSON.stringify(definition[name]))
          changed.push(name);
      }
      // Only upgrade the previous, narrowly scoped first-party client contract.
      if (changed.length) {
        if (
          changed.some((name) => !["grant_types", "scope"].includes(name)) ||
          JSON.stringify(client.grant_types) !== '["authorization_code"]' ||
          client.scope !== "openid"
        )
          throw new Error("Unexpected first-party client configuration");
        await requestJson(url, {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(definition),
        });
      }
    }
  })().catch((error) => {
    initialization = undefined;
    throw error;
  });
  return initialization;
}
