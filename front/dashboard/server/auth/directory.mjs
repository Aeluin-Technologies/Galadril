import { emailDomain } from "./policy.mjs";

export async function findConnection(directory, email) {
  const provider = await findLoginProvider(directory, email, false);
  if (provider.kind !== "sso" || !provider.connection)
    throw new Error("Registered provider required");
  return provider.connection;
}

/** Gateway owns provider discovery and deployment-specific local account routing. */
export async function findLoginProvider(directory, email, localEnabled) {
  emailDomain(email);
  return JSON.parse(
    await directory.request("loginProvider", { email, localEnabled }),
  );
}

export async function activeUser(directory, tenant, subject) {
  const user = await directory.request("activeUser", { tenant, user: subject });
  if (user !== subject) throw new Error("Identity changed");
  return user;
}

export function resolveIdentity(directory, connection, external, profile) {
  return directory.request("resolveIdentity", {
    domain: connection.domain,
    connection: connection.connection_id,
    external,
    profile: JSON.stringify(profile ?? {}),
  });
}
