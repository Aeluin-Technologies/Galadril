/** OAuth codes and secrets must never be forwarded through an HTTP redirect. */
import { record, text } from "../../shared/records.ts";
export class AuthenticationUpstreamError extends Error {
  constructor() {
    super("Authentication service temporarily unavailable");
  }
}

/**
 * Bound decoded upstream bodies before parsing; compressed Content-Length is insufficient.
 *
 * @throws {@link AuthenticationUpstreamError} when transport or response parsing is unavailable.
 */
export async function requestJson(
  url: unknown,
  options: RequestInit = {},
): Promise<unknown> {
  if (
    !(url instanceof URL) ||
    !["http:", "https:"].includes(url.protocol) ||
    url.username ||
    url.password ||
    url.hash
  )
    throw new Error("Invalid authentication endpoint");
  let response;
  try {
    response = await fetch(url, {
      ...options,
      redirect: "error",
      signal: AbortSignal.timeout(5000),
      headers: { accept: "application/json", ...options.headers },
    });
  } catch {
    throw new AuthenticationUpstreamError();
  }
  if (!response.ok) {
    if (response.status === 429 || response.status >= 500)
      throw new AuthenticationUpstreamError();
    throw new Error("Authentication request denied");
  }
  try {
    const body = response.body;
    if (!body) throw new AuthenticationUpstreamError();
    if (Number(response.headers.get("content-length")) > 65536) {
      await body.cancel();
      throw new AuthenticationUpstreamError();
    }
    const reader = body.getReader();
    const chunks = [];
    let size = 0;
    // Compressed or missing Content-Length values cannot bound decoded body memory.
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > 65536) {
          await reader.cancel();
          throw new AuthenticationUpstreamError();
        }
        chunks.push(value);
      }
    } finally {
      reader.releaseLock();
    }
    return JSON.parse(Buffer.concat(chunks, size).toString("utf8"));
  } catch {
    throw new AuthenticationUpstreamError();
  }
}

/** Object endpoints cannot silently accept a scalar or array response. */
export async function requestObject(
  url: URL,
  options: RequestInit = {},
): Promise<Record<string, unknown>> {
  const value = await requestJson(url, options);
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new AuthenticationUpstreamError();
  return record(value);
}

/** OAuth challenge acceptance stays on the configured private administrative origin. */
export function adminRequest(
  config: { admin: string },
  type: string,
  challenge: string,
  body?: Record<string, unknown>,
) {
  const url = new URL(
    `/admin/oauth2/auth/requests/${type}${body ? "/accept" : ""}`,
    config.admin,
  );
  url.searchParams.set(`${type}_challenge`, challenge);
  return requestObject(
    url,
    body
      ? {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        }
      : {},
  );
}

/** Polis owns the encrypted credential; never duplicate it in Gateway tables. */
export async function connectionCredential(
  config: { polis: string; polisApiKey: string },
  connection: { connection_id: string; tenant_id: string },
) {
  const url = new URL("/api/v1/sso", config.polis);
  url.searchParams.set("clientID", connection.connection_id);
  const records = await requestJson(url, {
    headers: { authorization: `Api-Key ${config.polisApiKey}` },
  });
  const credential = record(
    Array.isArray(records) && records.length === 1 ? records[0] : undefined,
  );
  if (
    credential.clientID !== connection.connection_id ||
    credential.tenant !== connection.tenant_id ||
    credential.product !== "galadril" ||
    typeof credential.clientSecret !== "string"
  )
    throw new Error("Invalid broker connection binding");
  return text(credential.clientSecret);
}
