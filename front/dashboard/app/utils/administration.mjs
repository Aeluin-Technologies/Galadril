const identifier = /^[a-zA-Z0-9_-]{1,64}$/;
const relationshipLabels = {
  member: "Has role",
  superior: "Includes access",
  reader: "Can view",
  editor: "Can edit",
  ingester: "Can add data",
  executor: "Can run",
  owner: "Owns",
  administrator: "Can manage",
};
const resourcePermissions = {
  group: ["reader", "ingester"],
  project: ["reader", "editor", "administrator"],
  table: ["reader", "editor", "administrator"],
  raw: ["reader"],
  document: ["reader", "editor", "administrator"],
  ontology: ["reader", "editor", "administrator"],
  pipeline: ["reader", "editor", "executor", "administrator"],
  conversation: ["reader", "editor", "administrator"],
  entity_state: ["reader", "editor"],
  event: ["reader"],
};
const permissionLabels = {
  reader: "View data",
  editor: "Edit data",
  ingester: "Add data",
  executor: "Run pipeline",
  administrator: "Manage resource",
};

/** Polis preserves upstream OIDC groups inside its verified raw attribute envelope. */
export function roleSyncDefaults(connectionId = "") {
  return { connectionId, claim: "raw.groups", enabled: false, mappings: [] };
}

/** Offer only the native relations accepted by the Gateway for this resource kind. */
export function resourceAccessOptions(kind) {
  return Object.hasOwn(resourcePermissions, kind)
    ? resourcePermissions[kind].map((value) => ({
        value,
        label: permissionLabels[value],
      }))
    : [];
}

/** A stale resource or incompatible relation must not enable an access change. */
export function canSetResourceAccess(kind, permission) {
  return (
    Object.hasOwn(resourcePermissions, kind) &&
    resourcePermissions[kind].includes(permission)
  );
}

/** Keep equivalent human names stable and make potential updates visible at review. */
export function accessRuleIdentifier(name) {
  const value = name
    .trim()
    .normalize("NFKD")
    .replace(/\p{M}+/gu, "")
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  if (!identifier.test(value))
    throw new Error(
      "Choose a rule name containing between 1 and 64 letters, numbers, hyphens or underscores",
    );
  return value;
}

/** SpiceDB makes senior users members of the included junior role, not vice versa. */
export function roleInclusion(selectedRole, includedRole, enabled) {
  id(selectedRole);
  id(includedRole);
  if (selectedRole === includedRole || typeof enabled !== "boolean")
    throw new Error("Choose two different roles");
  return { role: includedRole, parent: selectedRole, enabled };
}

const failureMessages = {
  SELF_SUSPENSION:
    "You cannot suspend your own account. Ask another administrator to manage your access.",
  LAST_ADMINISTRATOR:
    "You cannot remove the last active administrator. Add another administrator first.",
  FORBIDDEN:
    "Your administrator access does not allow this change. Ask an organization administrator to review your access.",
  UNAUTHENTICATED:
    "Your session needs verification. Sign in again, then retry this change.",
  INVALID_INPUT:
    "Some settings are not valid. Review the highlighted fields and try again.",
  CONFLICT:
    "These settings changed or already exist. Reload the relevant list, then review your change.",
  DOMAIN_NOT_VERIFIED:
    "Your company domain is not verified yet. Publish the DNS record and wait for it to become available.",
  PROVIDER_UNAVAILABLE:
    "Your company sign-in provider could not be reached. Check its address and try again.",
  PERMISSION_IMPORT_DISABLED:
    "Permission imports are disabled. Enable them before applying this configuration.",
  MFA_REQUIRED:
    "Confirm your identity with your passkey before making this change.",
  MANAGED_BY_PROVIDER:
    "Your company sign-in provider manages this role. Update its group match under automatic imports before removing it here.",
};

class AdministrationFailure extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

function incompatibleSchema(errors) {
  return (
    Array.isArray(errors) &&
    errors.some(
      (error) =>
        typeof error?.message === "string" &&
        /^Unknown (field|argument|type)\b/.test(error.message),
    )
  );
}

/** Display only bounded Gateway classifications; backend details may contain secrets. */
export function administrationError(
  error,
  fallback,
  incompatibleMessage = fallback,
) {
  if (
    error?.code === "SCHEMA_MISMATCH" ||
    incompatibleSchema(error?.data?.errors)
  )
    return incompatibleMessage;
  if (error instanceof AdministrationFailure)
    return error.code === "INVALID_INPUT" ? fallback : error.message;
  const code = error?.data?.errors?.[0]?.extensions?.code;
  return failureMessages[code] || fallback;
}

/** Reject partial GraphQL responses so an incomplete mutation cannot appear successful. */
export function administrationResult(response) {
  if (response?.errors?.length) {
    if (incompatibleSchema(response.errors))
      throw new AdministrationFailure(
        "SCHEMA_MISMATCH",
        "Incompatible administration service",
      );
    const code = response.errors[0]?.extensions?.code;
    if (failureMessages[code])
      throw new AdministrationFailure(code, failureMessages[code]);
    throw new Error("Administration request failed");
  }
  if (
    !response?.data?.administration ||
    typeof response.data.administration !== "object" ||
    Array.isArray(response.data.administration)
  )
    throw new Error("Missing administration response");
  return response.data.administration;
}

/** Omit dangling edges rather than exposing labels outside the server-filtered node set. */
export function accessGraphLayout(graph, resourceNames = new Map()) {
  if (
    !Array.isArray(graph?.nodes) ||
    !Array.isArray(graph?.edges) ||
    graph.nodes.length > 1000 ||
    graph.edges.length > 2000
  )
    throw new Error("The access map could not be read");
  const columns = {
    user: 0,
    role: 1,
    group: 2,
    project: 2,
    table: 3,
    raw: 3,
    document: 3,
    ontology: 3,
    pipeline: 3,
    conversation: 3,
    entity_state: 3,
    event: 3,
  };
  const counts = [0, 0, 0, 0];
  const ids = new Set();
  const nodes = graph.nodes.map((node) => {
    if (
      typeof node.id !== "string" ||
      typeof node.label !== "string" ||
      !Object.hasOwn(columns, node.kind) ||
      ids.has(node.id)
    )
      throw new Error("The access map contains an invalid account or resource");
    ids.add(node.id);
    const column = columns[node.kind];
    return {
      id: node.id,
      label: resourceNames.get(`${node.kind}:${node.resourceId}`) ?? node.label,
      position: { x: column * 360, y: counts[column]++ * 150 },
      style: { width: "200px", minHeight: "72px" },
      data: { kind: node.kind, resourceId: node.resourceId },
      sourcePosition: "right",
      targetPosition: "left",
      class: `access-node access-node-${node.kind}`,
    };
  });
  const connections = new Map();
  for (const edge of graph.edges.filter(
    (edge) =>
      ids.has(edge.source) &&
      ids.has(edge.target) &&
      typeof edge.relation === "string",
  )) {
    const key = JSON.stringify([edge.source, edge.target]);
    const connection = connections.get(key);
    if (connection) {
      if (!connection.relations.includes(edge.relation))
        connection.relations.push(edge.relation);
    } else
      connections.set(key, {
        source: edge.source,
        target: edge.target,
        relations: [edge.relation],
      });
  }
  const edges = Array.from(connections.values(), (edge, index) => ({
    id: `access-${index}`,
    source: edge.source,
    target: edge.target,
    label:
      edge.relations.length > 1
        ? `${edge.relations.length} permissions`
        : Object.hasOwn(relationshipLabels, edge.relations[0])
          ? relationshipLabels[edge.relations[0]]
          : "Connected to",
    data: { relations: edge.relations },
    type: "smoothstep",
    style: { stroke: "rgb(var(--ui-primary-foreground))" },
  }));
  return { nodes, edges, truncated: graph.truncated === true };
}

/** The Gateway converts validated blocks into Cedar; the browser does not author policy semantics. */
export function generalAccessDraft({ roles, start, end, regions = "" }) {
  if (!Array.isArray(roles) || roles.length > 64)
    throw new Error("Choose up to 64 roles");
  for (const role of roles) id(role);
  const configuration = { roleNames: [...new Set(roles)], regions: [] };
  if (start || end) {
    if (!/^\d{2}:00$/.test(start) || !/^\d{2}:00$/.test(end))
      throw new Error("Choose whole hours for both the start and end time");
    const startHour = Number(start.slice(0, 2));
    const endHour = Number(end.slice(0, 2)) || 24;
    if (startHour > 23 || endHour > 24 || startHour === endHour)
      throw new Error("Choose different valid start and end hours");
    configuration.startHourUtc = startHour;
    configuration.endHourUtc = endHour;
  }
  if (regions.trim()) {
    const values = regions
      .split(/[\s,]+/u)
      .filter(Boolean)
      .map((value) => value.toUpperCase());
    if (values.length > 64 || values.some((value) => !/^[A-Z]{2}$/.test(value)))
      throw new Error("Use two-letter country codes for each region");
    configuration.regions = [...new Set(values)];
  }
  if (!start && !configuration.regions.length)
    throw new Error("Add a time window or a region before reviewing this rule");
  return configuration;
}

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

/** Translate the import file format into explicit GraphQL input choices. */
export function permissionImportInput(value) {
  shape(value, ["version", "operations"]);
  if (
    value.version !== 1 ||
    !Array.isArray(value.operations) ||
    !value.operations.length ||
    value.operations.length > 64
  )
    throw new Error("Unsupported permission manifest");
  return {
    version: 1,
    operations: value.operations.map((item) => {
      if (item.kind === "role_assignment") {
        shape(item, ["kind", "user", "role", "enabled"]);
        id(item.user);
        id(item.role);
        if (typeof item.enabled !== "boolean")
          throw new Error("Choose grant or remove");
        return {
          roleAssignment: {
            user: item.user,
            role: item.role,
            enabled: item.enabled,
          },
        };
      }
      if (item.kind === "relationship") {
        shape(item, ["kind", "resource", "relation", "subject", "enabled"]);
        shape(item.resource, ["type", "id"]);
        shape(item.subject, ["type", "id"]);
        if (typeof item.enabled !== "boolean")
          throw new Error("Choose grant or remove");
        return {
          relationship: {
            resource: { kind: item.resource.type, id: item.resource.id },
            relation: item.relation,
            subject: { kind: item.subject.type, id: item.subject.id },
            enabled: item.enabled,
          },
        };
      }
      if (item.kind === "cedar_policy") {
        shape(item, ["kind", "id", "content", "active"]);
        id(item.id);
        if (
          typeof item.content !== "string" ||
          typeof item.active !== "boolean"
        )
          throw new Error("Invalid policy");
        return {
          cedarPolicy: {
            id: item.id,
            content: item.content,
            active: item.active,
          },
        };
      }
      throw new Error("Unsupported permission change");
    }),
  };
}
