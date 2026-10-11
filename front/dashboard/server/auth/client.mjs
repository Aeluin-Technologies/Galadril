import { requestJson } from "./http.mjs";

export function clientDefinition(config) {
  return {
    client_id: "galadril-studio",
    client_secret: config.clientSecret,
    client_name: "Galadril Studio",
    grant_types: ["authorization_code"],
    response_types: ["code"],
    scope: "openid",
    audience: ["galadril"],
    redirect_uris: [`${config.origin}/auth/callback`],
    token_endpoint_auth_method: "client_secret_basic",
    access_token_strategy: "jwt",
    skip_consent: false,
  };
}

let initialization;

/** Reconcile the first-party client using the private administrative API. */
export function ensureClient(config) {
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
      const client = await response.json();
      for (const name of [
        "redirect_uris",
        "grant_types",
        "response_types",
        "audience",
        "scope",
        "token_endpoint_auth_method",
        "access_token_strategy",
        "skip_consent",
      ]) {
        if (JSON.stringify(client[name]) !== JSON.stringify(definition[name]))
          throw new Error("Unexpected first-party client configuration");
      }
    }
  })().catch((error) => {
    initialization = undefined;
    throw error;
  });
  return initialization;
}
