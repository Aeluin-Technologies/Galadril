const identifier = /^[a-zA-Z0-9_-]{1,64}$/;

/** Canonicalize convenient input before ownership verification; never infer a different domain. */
export function normalizeCompanyDomain(value) {
  let domain = value.trim();
  if (/^https?:\/\//i.test(domain)) {
    const website = new URL(domain);
    if (website.username || website.password)
      throw new Error(
        "Enter a company domain or work email without credentials",
      );
    domain = website.hostname;
  } else if (domain.includes("@")) {
    const parts = domain.split("@");
    if (parts.length !== 2 || !/^[^\s@]+$/.test(parts[0]))
      throw new Error("Enter one work email or company domain");
    domain = parts[1];
  }
  domain = domain.replace(/\.$/, "");
  if (!domain || /[\s/\\:?#@]/.test(domain))
    throw new Error("Enter a company domain such as company.com");
  domain = new URL(`https://${domain}`).hostname;
  const labels = domain.split(".");
  if (
    domain.length > 253 ||
    labels.length < 2 ||
    /^\d+(?:\.\d+){3}$/.test(domain) ||
    labels.some(
      (label) => !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label),
    )
  )
    throw new Error("Enter a company domain such as company.com");
  return domain;
}

/** Preserve issuer path case and trailing slashes because OIDC compares issuer values exactly. */
export function normalizeProviderAddress(value) {
  let address = value.trim();
  if (!address || /[\s\\?#]/.test(address))
    throw new Error("Enter your provider's HTTPS sign-in address");
  if (!/^[a-z][a-z\d+.-]*:\/\//i.test(address)) address = `https://${address}`;
  const provider = new URL(address);
  if (provider.protocol !== "https:" || provider.username || provider.password)
    throw new Error("Enter an HTTPS address without embedded credentials");
  const discovery = "/.well-known/openid-configuration";
  const path = provider.pathname.endsWith(discovery)
    ? provider.pathname.slice(0, -discovery.length)
    : provider.pathname === "/" && !address.endsWith("/")
      ? ""
      : provider.pathname;
  return provider.origin + path;
}

/** SAML metadata can carry query parameters; issuer values cannot. */
export function normalizeDiscoveryAddress(value) {
  let address = value.trim();
  if (!/^[a-z][a-z\d+.-]*:\/\//i.test(address)) address = `https://${address}`;
  const url = new URL(address);
  if (
    url.protocol !== "https:" ||
    url.username ||
    url.password ||
    url.hash ||
    /\s/.test(address) ||
    address.length > 2048
  )
    throw new Error("Enter a public HTTPS provider or metadata address");
  return url.pathname === "/" && !url.search && !address.endsWith("/")
    ? url.origin
    : url.href;
}

/** A preview never authorizes saving a connection or carrying over app secrets. */
export function providerFormValues(configuration) {
  if (
    configuration?.protocol === "oidc" &&
    typeof configuration.issuer === "string" &&
    configuration.issuer.startsWith("https://")
  )
    return {
      protocol: "oidc",
      issuer: normalizeProviderAddress(configuration.issuer),
      metadata: "",
    };
  if (
    configuration?.protocol === "saml" &&
    typeof configuration.saml_metadata === "string" &&
    configuration.saml_metadata.trim() &&
    configuration.saml_metadata.length <= 131072
  )
    return {
      protocol: "saml",
      issuer: "",
      metadata: configuration.saml_metadata,
    };
  throw new Error("Unsupported detected provider");
}

function shape(value, fields) {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    Object.keys(value).some((key) => !fields.includes(key)) ||
    fields.some((key) => !(key in value))
  )
    throw new Error("Unsupported permission block");
}
function id(value) {
  if (typeof value !== "string" || !identifier.test(value))
    throw new Error("Choose a valid account, role or data space");
}

/** Visual editing must preserve the exact native import contract and tenant boundary. */
export function permissionManifest(operations) {
  if (
    !Array.isArray(operations) ||
    !operations.length ||
    operations.length > 64
  )
    throw new Error("Add between 1 and 64 permission changes");
  for (const item of operations) {
    if (item.kind === "role_assignment") {
      shape(item, ["kind", "user", "role", "enabled"]);
      id(item.user);
      id(item.role);
      if (typeof item.enabled !== "boolean")
        throw new Error("Choose grant or remove");
    } else if (item.kind === "relationship") {
      shape(item, ["kind", "resource", "relation", "subject", "enabled"]);
      shape(item.resource, ["type", "id"]);
      shape(item.subject, ["type", "id"]);
      if (
        item.resource.type !== "group" ||
        item.subject.type !== "user" ||
        !["reader", "ingester"].includes(item.relation) ||
        typeof item.enabled !== "boolean"
      )
        throw new Error("Unsupported data access change");
      id(item.resource.id);
      id(item.subject.id);
    } else if (item.kind === "cedar_policy") {
      shape(item, ["kind", "id", "content", "active"]);
      id(item.id);
      if (
        typeof item.active !== "boolean" ||
        typeof item.content !== "string" ||
        !item.content.trim() ||
        item.content.length > 8192
      )
        throw new Error("Enter a valid Cedar policy");
    } else throw new Error("Unsupported permission block");
  }
  return { version: 1, operations };
}

/** Never drop code fields while switching to the visual editor. */
export function permissionBlocks(code) {
  const value = JSON.parse(code);
  shape(value, ["version", "operations"]);
  if (value.version !== 1) throw new Error("Unsupported manifest version");
  return permissionManifest(value.operations).operations;
}
