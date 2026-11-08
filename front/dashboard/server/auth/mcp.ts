import { record } from "#shared/records.ts";
import { requestJson } from "./http.ts";

export interface McpClient {
  id: string;
  name: string;
  redirectUris: string[];
  resource: string;
  scopes: string[];
}

function secureUrl(value: unknown): string {
  if (typeof value !== "string" || value.length > 2048)
    throw new Error("Invalid MCP URL");
  const url = new URL(value);
  if (url.protocol !== "https:" || url.username || url.password || url.hash)
    throw new Error("MCP requires HTTPS");
  return value;
}

/** Only deployment policy may register OAuth clients; discovery never grants access. */
export function parseMcpClients(encoded: string): McpClient[] {
  if (!encoded) return [];
  if (encoded.length > 32768)
    throw new Error("MCP client policy exceeds limit");
  const rows: unknown = JSON.parse(encoded);
  if (!Array.isArray(rows) || rows.length > 32)
    throw new Error("Invalid MCP client policy");
  const clients = rows.map((value: unknown): McpClient => {
    const item = record(value);
    if (
      typeof item.id !== "string" ||
      !/^[a-zA-Z0-9_-]{1,128}$/.test(item.id) ||
      item.id === "galadril-studio" ||
      typeof item.name !== "string" ||
      !item.name.trim() ||
      item.name.length > 128 ||
      !Array.isArray(item.redirectUris) ||
      !item.redirectUris.length ||
      item.redirectUris.length > 8 ||
      !Array.isArray(item.scopes) ||
      !item.scopes.length ||
      item.scopes.length > 32 ||
      item.scopes.some(
        (scope: unknown) =>
          typeof scope !== "string" ||
          !/^[a-zA-Z0-9:_-]{1,128}$/.test(scope) ||
          ["openid", "offline_access"].includes(scope),
      )
    )
      throw new Error("Invalid MCP client policy");
    const redirectUris = item.redirectUris.map(secureUrl);
    const scopes: string[] = item.scopes.map((scope: unknown) => String(scope));
    const resource = secureUrl(item.resource);
    const resourceUrl = new URL(resource);
    if (
      resourceUrl.search ||
      new Set(scopes).size !== scopes.length ||
      new Set(redirectUris).size !== redirectUris.length
    )
      throw new Error("Invalid MCP resource or repeated policy");
    return { id: item.id, name: item.name, redirectUris, resource, scopes };
  });
  if (new Set(clients.map((client) => client.id)).size !== clients.length)
    throw new Error("Duplicate MCP OAuth client");
  return clients;
}

export function mcpAuthorization(query: URLSearchParams, clients: McpClient[]) {
  for (const key of query.keys())
    if (query.getAll(key).length !== 1)
      throw new Error("Repeated OAuth parameter");
  const client = clients.find((item) => item.id === query.get("client_id"));
  const scope = (query.get("scope") ?? client?.scopes.join(" ") ?? "").split(
    " ",
  );
  if (
    !client ||
    query.get("resource") !== client.resource ||
    query.get("response_type") !== "code" ||
    !client.redirectUris.includes(query.get("redirect_uri") ?? "") ||
    query.get("code_challenge_method") !== "S256" ||
    !/^[a-zA-Z0-9_-]{43,128}$/.test(query.get("code_challenge") ?? "") ||
    !query.get("state")?.length ||
    (query.get("state")?.length ?? 0) > 2048 ||
    scope.length > 34 ||
    new Set(scope).size !== scope.length ||
    !scope.some((item) => client.scopes.includes(item)) ||
    scope.some(
      (item) => ![...client.scopes, "openid", "offline_access"].includes(item),
    )
  )
    throw new Error("Invalid MCP authorization");
  const parameters = new URLSearchParams();
  for (const key of [
    "client_id",
    "redirect_uri",
    "response_type",
    "state",
    "code_challenge",
    "code_challenge_method",
    "nonce",
  ])
    if (query.has(key)) parameters.set(key, query.get(key)!);
  parameters.set("scope", scope.join(" "));
  // Hydra 2.x ignores resource indicators; the trusted bridge binds its audience instead.
  parameters.set("audience", client.resource);
  return { client, parameters };
}

export function mcpConsent(value: unknown, clients: McpClient[]) {
  const request = record(value);
  const client = clients.find(
    (item) => item.id === record(request.client).client_id,
  );
  if (
    !client ||
    !Array.isArray(request.requested_scope) ||
    !request.requested_scope.length ||
    !request.requested_scope.some((scope: unknown) =>
      client.scopes.includes(String(scope)),
    ) ||
    request.requested_scope.some(
      (scope: unknown) =>
        typeof scope !== "string" ||
        ![...client.scopes, "openid", "offline_access"].includes(scope),
    ) ||
    new Set(request.requested_scope).size !== request.requested_scope.length ||
    !Array.isArray(request.requested_access_token_audience) ||
    request.requested_access_token_audience.length !== 1 ||
    request.requested_access_token_audience[0] !== client.resource
  )
    throw new Error("Invalid MCP consent");
  return {
    scope: request.requested_scope.map((scope: unknown) => String(scope)),
    audience: [client.resource],
  };
}

export function mcpClientDefinition(client: McpClient) {
  return {
    client_id: client.id,
    client_name: client.name,
    redirect_uris: client.redirectUris,
    grant_types: ["authorization_code", "refresh_token"],
    response_types: ["code"],
    scope: [...client.scopes, "openid", "offline_access"].join(" "),
    audience: [client.resource],
    token_endpoint_auth_method: "none",
    access_token_strategy: "jwt",
    skip_consent: false,
  };
}

/** Refuse unexpected registrations rather than rewriting an existing client's privileges. */
export async function ensureMcpClient(client: McpClient, admin: string) {
  const definition = mcpClientDefinition(client);
  const endpoint = new URL(
    `/admin/clients/${encodeURIComponent(client.id)}`,
    admin,
  );
  const response = await fetch(endpoint, {
    redirect: "error",
    signal: AbortSignal.timeout(5000),
  });
  if (response.status === 404) {
    await requestJson(new URL("/admin/clients", admin), {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(definition),
    });
    return;
  }
  if (!response.ok) throw new Error("MCP OAuth registration unavailable");
  const existing = record(await response.json());
  for (const field of Object.keys(definition) as (keyof typeof definition)[])
    if (JSON.stringify(existing[field]) !== JSON.stringify(definition[field]))
      throw new Error("MCP OAuth registration differs from deployment policy");
}
