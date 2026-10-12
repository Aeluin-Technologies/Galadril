import { createServer } from "node:http";

// This double exercises UI contracts; Gateway authorization has separate real-service tests.
const states = new Map();
createServer(async (request, response) => {
  response.setHeader("content-type", "application/json");
  const actor = request.headers["x-galadril-sub"];
  if (
    !actor ||
    request.headers["x-galadril-tenant"] !== "demo" ||
    actor === "browser-member"
  ) {
    response.end(
      JSON.stringify({
        errors: [{ message: "Administrator access required" }],
      }),
    );
    return;
  }
  if (!states.has(actor))
    states.set(actor, {
      settings: {
        organization_name: "Browser test organization",
        passkey_mfa: actor !== "browser-enable-passkey",
        permission_import: false,
        connections: [],
      },
      aliceActive: true,
      roles: [{ roleName: "reader" }],
      assignments: [],
      policies: [],
      synchronization: [],
      hierarchy: [],
    });
  const state = states.get(actor);
  try {
    let text = "";
    for await (const chunk of request) text += chunk;
    const { query, variables = {} } = JSON.parse(text);
    let data;
    if (query.includes("ssoSettings"))
      data = { ssoSettings: JSON.stringify(state.settings) };
    else if (query.includes("users("))
      data = {
        users: [
          {
            userId: actor,
            displayName: "Browser administrator",
            email: `${actor}@demo.invalid`,
            isActive: true,
          },
          {
            userId: "alice",
            displayName: "Alice Example",
            email: "alice@demo.invalid",
            isActive: state.aliceActive,
          },
        ],
        roles: state.roles,
        roleAssignments: state.assignments,
      };
    else if (query.includes("roles("))
      data = {
        roles: state.roles.filter(
          (role) =>
            !variables.search || role.roleName.includes(variables.search),
        ),
      };
    else if (query.includes("accessGraph"))
      data = {
        accessGraph: JSON.stringify({
          nodes: [
            {
              id: "user:alice",
              kind: "user",
              label: "Alice Example",
              resourceId: "alice",
            },
            ...state.roles.map((role) => ({
              id: `role:${role.roleName}`,
              kind: "role",
              label: role.roleName,
              resourceId: role.roleName,
            })),
            {
              id: "document:report",
              kind: "document",
              label: "Quarterly report",
              resourceId: "report",
            },
          ],
          edges: [
            ...state.hierarchy,
            {
              source: "role:reader",
              target: "document:report",
              relation: "reader",
            },
          ],
          truncated: false,
        }),
      };
    else if (query.includes("cedarPolicies"))
      data = { cedarPolicies: JSON.stringify(state.policies) };
    else if (query.includes("roleSyncConfigurations"))
      data = { roleSyncConfigurations: JSON.stringify(state.synchronization) };
    else if (query.includes("requestSsoDomain"))
      data = {
        requestSsoDomain: JSON.stringify({
          record: `_galadril.${variables.domain}`,
          value: "fixture-dns-challenge",
        }),
      };
    else if (query.includes("detectSsoProvider"))
      data = {
        detectSsoProvider: JSON.stringify({
          domain: variables.domain,
          domain_verified: true,
          configuration: {
            protocol: "oidc",
            issuer: "https://login.example.com",
            sign_in_url: "https://login.example.com/authorize",
            jwks_uri: "https://login.example.com/keys",
            automatic_registration: true,
          },
        }),
      };
    else if (query.includes("configureSsoConnection")) {
      const configuration = JSON.parse(variables.configuration);
      state.settings.connections.push({
        domain: configuration.domain,
        protocol: "oidc",
        enabled: true,
        jit_provision: configuration.jit_provision,
        connection_id: `connection-${configuration.domain}`,
      });
      data = { configureSsoConnection: true };
    } else if (query.includes("setPermissionImport")) {
      state.settings.permission_import = variables.enabled;
      data = { setPermissionImport: true };
    } else if (query.includes("setPasskeyMfa")) {
      state.settings.passkey_mfa = variables.enabled;
      data = { setPasskeyMfa: true };
    } else if (query.includes("configureRoleSync")) {
      const configuration = JSON.parse(variables.configuration);
      state.synchronization = [configuration];
      data = { configureRoleSync: true };
    } else if (query.includes("importPermissions")) {
      if (!state.settings.permission_import)
        throw new Error("Permission imports disabled");
      data = {
        importPermissions: JSON.stringify({
          applied: JSON.parse(variables.manifest).operations.length,
          complete: true,
          failed_at: null,
        }),
      };
    } else if (query.includes("createRole")) {
      state.roles.push({ roleName: variables.name });
      data = { createRole: true };
    } else if (query.includes("setRoleParent")) {
      state.hierarchy = state.hierarchy.filter(
        (edge) =>
          edge.source !== `role:${variables.parent}` ||
          edge.target !== `role:${variables.role}`,
      );
      if (variables.enabled)
        state.hierarchy.push({
          source: `role:${variables.parent}`,
          target: `role:${variables.role}`,
          relation: "superior",
        });
      data = { setRoleParent: true };
    } else if (query.includes("configureGeneralAccess")) {
      state.policies = state.policies.filter(
        (policy) => policy.id !== variables.id,
      );
      state.policies.push({
        id: variables.id,
        content:
          "forbid(principal, action, resource) when { context.hour_utc < 8 };",
        active: true,
      });
      data = { configureGeneralAccess: true };
    } else if (query.includes("setCedarPolicy")) {
      data = { setCedarPolicy: true };
    } else if (query.includes("resetUserPasskeys"))
      data = { resetUserPasskeys: true };
    else if (query.includes("updateUser")) {
      if (variables.user !== "alice")
        throw new Error("Fixture only changes Alice");
      state.aliceActive = variables.active;
      data = { updateUser: true };
    } else if (query.includes("updateOrganization")) {
      state.settings.organization_name = variables.name;
      data = { updateOrganization: true };
    } else {
      // Unrelated dashboard reads intentionally return no fixture business data.
      response.end(
        JSON.stringify({
          errors: [{ message: "Unsupported fixture operation" }],
        }),
      );
      return;
    }
    response.end(
      JSON.stringify({
        data: query.includes("administration")
          ? { administration: data }
          : data,
      }),
    );
  } catch {
    response.statusCode = 400;
    response.end(
      JSON.stringify({ errors: [{ message: "Invalid fixture request" }] }),
    );
  }
}).listen(8081, "127.0.0.1");
