import { emailDomain } from "./policy.ts";
import type { AuthConnection, AuthDirectory } from "./contracts.ts";
import { parseObject, record, text } from "#shared/records.ts";

/** Local routing must never substitute for an explicitly registered enterprise connection. */
export async function findConnection(directory: AuthDirectory, email: string) {
  const provider = await findLoginProvider(directory, email, false);
  if (provider.kind !== "sso" || !provider.connection)
    throw new Error("Registered provider required");
  return provider.connection;
}

/** Gateway owns provider discovery and deployment-specific local account routing. */
export async function findLoginProvider(
  directory: AuthDirectory,
  email: string,
  localEnabled: boolean,
): Promise<{ kind: "local" } | { kind: "sso"; connection: AuthConnection }> {
  emailDomain(email);
  const provider = parseObject(
    await directory.request("loginProvider", { email, localEnabled }),
  );
  if (provider.kind === "local") return { kind: "local" };
  if (provider.kind !== "sso") throw new Error("Registered provider required");
  const connection = record(provider.connection);
  return {
    kind: "sso",
    connection: {
      domain: text(connection.domain),
      connection_id: text(connection.connection_id),
      tenant_id: text(connection.tenant_id),
    },
  };
}

/** Recheck mutable IAM state rather than treating a valid token as current authorization. */
export async function activeUser(
  directory: AuthDirectory,
  tenant: string,
  subject: string,
) {
  const user = await directory.request("activeUser", { tenant, user: subject });
  if (user !== subject) throw new Error("Identity changed");
  return user;
}

/** Canonical identity bindings belong to Gateway, never to provider-controlled profile text. */
export async function resolveIdentity(
  directory: AuthDirectory,
  connection: AuthConnection,
  external: string,
  profile: unknown,
) {
  return text(
    await directory.request("resolveIdentity", {
      domain: connection.domain,
      connection: connection.connection_id,
      external,
      profile: JSON.stringify(profile ?? {}),
    }),
  );
}
