/** OAuth codes and secrets must never be forwarded through an HTTP redirect. */
export class AuthenticationUpstreamError extends Error {
  constructor() {
    super("Authentication service temporarily unavailable");
  }
}

export async function requestJson(url, options = {}) {
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

export function adminRequest(config, type, challenge, body) {
  const url = new URL(
    `/admin/oauth2/auth/requests/${type}${body ? "/accept" : ""}`,
    config.admin,
  );
  url.searchParams.set(`${type}_challenge`, challenge);
  return requestJson(
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
export async function connectionCredential(config, connection) {
  const url = new URL("/api/v1/sso", config.polis);
  url.searchParams.set("clientID", connection.connection_id);
  const records = await requestJson(url, {
    headers: { authorization: `Api-Key ${config.polisApiKey}` },
  });
  const record =
    Array.isArray(records) && records.length === 1 ? records[0] : undefined;
  if (
    record?.clientID !== connection.connection_id ||
    record?.tenant !== connection.tenant_id ||
    record?.product !== "galadril" ||
    typeof record?.clientSecret !== "string"
  )
    throw new Error("Invalid broker connection binding");
  return record.clientSecret;
}
