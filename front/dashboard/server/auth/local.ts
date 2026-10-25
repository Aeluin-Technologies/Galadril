import { requestObject } from "./http.ts";
import { record, text } from "#shared/records.ts";
import type { AuthConfig } from "./config.ts";

/** Bind local authentication to immutable administrator-owned IAM references. */
export function localSessionIdentity(
  value: unknown,
  startedAt: unknown,
  now = Date.now(),
) {
  const session = record(value);
  const identity = record(session.identity);
  const binding = record(record(identity.metadata_admin).galadril);
  const authenticated = Date.parse(
    typeof session.authenticated_at === "string"
      ? session.authenticated_at
      : "",
  );
  const expires = Date.parse(
    typeof session.expires_at === "string" ? session.expires_at : "",
  );
  if (
    typeof startedAt !== "number" ||
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
      typeof identity.id === "string" ? identity.id : "",
    ) ||
    !/^[a-zA-Z0-9_-]{1,64}$/.test(
      typeof binding.tenant_id === "string" ? binding.tenant_id : "",
    ) ||
    !/^[a-zA-Z0-9_-]{1,64}$/.test(
      typeof binding.user_id === "string" ? binding.user_id : "",
    )
  )
    throw new Error("Invalid local identity session");
  return {
    tenant: text(binding.tenant_id),
    subject: text(binding.user_id),
    external: text(identity.id),
  };
}

/** Forward only the identity session, keeping Galadril JWTs inside the BFF. */
export function identityCookie(cookies = "") {
  const values = cookies
    .split(";")
    .map((value) => value.trim())
    .filter((value) => value.startsWith("__Host-galadril-identity="));
  if (values.length !== 1) throw new Error("Missing identity session");
  return values[0] ?? "";
}

export async function localIdentity(
  config: Pick<AuthConfig, "kratos" | "kratosAdmin"> & {
    directory: import("./contracts.ts").AuthDirectory;
  },
  cookies: string | undefined,
  startedAt: unknown,
) {
  if (!config.kratos) throw new Error("Local login disabled");
  const session = await requestObject(
    new URL("/sessions/whoami", config.kratos),
    {
      headers: { cookie: identityCookie(cookies) },
    },
  );
  const id = record(session.identity).id;
  if (typeof id !== "string" || !/^[0-9a-f-]{36}$/.test(id))
    throw new Error("Invalid identity reference");
  // Kratos intentionally removes administrator metadata from public sessions.
  const identityRecord = await requestObject(
    new URL(`/admin/identities/${id}`, config.kratosAdmin),
  );
  if (identityRecord.id !== id) throw new Error("Identity reference changed");
  const identity = localSessionIdentity(
    { ...session, identity: identityRecord },
    startedAt,
  );
  const bound = await config.directory.request("bindLocal", {
    tenant: identity.tenant,
    user: identity.subject,
    external: identity.external,
    profile: JSON.stringify(identityRecord.traits ?? {}),
  });
  if (bound !== true) throw new Error("Local identity is not provisioned");
  return identity;
}
