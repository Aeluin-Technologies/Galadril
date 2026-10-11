/** OAuth codes and secrets must never be forwarded through an HTTP redirect. */
export async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    redirect: "error",
    signal: AbortSignal.timeout(5000),
    headers: { accept: "application/json", ...options.headers },
  });
  if (!response.ok) throw new Error("Authentication upstream unavailable");
  const text = await response.text();
  if (text.length > 65536) throw new Error("Authentication response too large");
  return JSON.parse(text);
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
