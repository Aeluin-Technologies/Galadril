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
        passkey_mfa: true,
        permission_import: false,
        connections: [],
      },
      aliceActive: true,
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
        roles: [{ roleName: "reader" }],
        roleAssignments: [],
      };
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
      });
      data = { configureSsoConnection: true };
    } else if (query.includes("setPermissionImport")) {
      state.settings.permission_import = variables.enabled;
      data = { setPermissionImport: true };
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
    response.end(JSON.stringify({ data }));
  } catch {
    response.statusCode = 400;
    response.end(
      JSON.stringify({ errors: [{ message: "Invalid fixture request" }] }),
    );
  }
}).listen(8081, "127.0.0.1");
