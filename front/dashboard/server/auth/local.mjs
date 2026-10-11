import { requestJson } from "./http.mjs";

/** Bind local authentication to immutable administrator-owned IAM references. */
export function localSessionIdentity(session, startedAt, now = Date.now()) {
  const identity = session?.identity;
  const binding = identity?.metadata_admin?.galadril;
  const authenticated = Date.parse(session?.authenticated_at);
  const expires = Date.parse(session?.expires_at);
  if (
    !Number.isFinite(startedAt) ||
    startedAt > now ||
    session?.active !== true ||
    identity?.state !== "active" ||
    !Number.isFinite(authenticated) ||
    authenticated < startedAt - 1000 ||
    authenticated > now + 1000 ||
    !Number.isFinite(expires) ||
    expires <= now ||
    !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(
      identity?.id ?? "",
    ) ||
    !/^[a-zA-Z0-9_-]{1,64}$/.test(binding?.tenant_id ?? "") ||
    !/^[a-zA-Z0-9_-]{1,64}$/.test(binding?.user_id ?? "")
  )
    throw new Error("Invalid local identity session");
  return {
    tenant: binding.tenant_id,
    subject: binding.user_id,
    external: identity.id,
  };
}

/** Forward only the identity session, keeping Galadril JWTs inside the BFF. */
export function identityCookie(cookies = "") {
  const values = cookies
    .split(";")
    .map((value) => value.trim())
    .filter((value) => value.startsWith("__Host-galadril-identity="));
  if (values.length !== 1) throw new Error("Missing identity session");
  return values[0];
}

export async function localIdentity(config, cookies, startedAt) {
  if (!config.kratos) throw new Error("Local login disabled");
  const session = await requestJson(
    new URL("/sessions/whoami", config.kratos),
    {
      headers: { cookie: identityCookie(cookies) },
    },
  );
  const id = session?.identity?.id;
  if (!/^[0-9a-f-]{36}$/.test(id ?? ""))
    throw new Error("Invalid identity reference");
  // Kratos intentionally removes administrator metadata from public sessions.
  const record = await requestJson(
    new URL(`/admin/identities/${id}`, config.kratosAdmin),
  );
  if (record.id !== id) throw new Error("Identity reference changed");
  const identity = localSessionIdentity(
    { ...session, identity: record },
    startedAt,
  );
  const bound = await config.directory.request("bindLocal", {
    tenant: identity.tenant,
    user: identity.subject,
    external: identity.external,
    profile: JSON.stringify(record.traits ?? {}),
  });
  if (bound !== true) throw new Error("Local identity is not provisioned");
  return identity;
}
