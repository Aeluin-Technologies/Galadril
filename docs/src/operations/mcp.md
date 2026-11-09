# Authenticated MCP hub

The MCP server belongs to `services/mcp`; shared authorization and the native
client belong to `platform/mcp`. Plugins declare client capabilities and never
host public servers. The first provider supplies Légifrance and Judilibre through
PISTE. Installed providers register tools in separately authorized namespaces.

## Authority

Public clients use the Galadril login and OAuth authorization code with PKCE
S256. Configure `AUTH_MCP_CLIENTS` on the dashboard with a JSON array:

```json
[
  {
    "id": "galadril-mcp-chatgpt-law",
    "name": "Galadril legal research",
    "redirectUris": ["https://REPLACE-WITH-THE-EXACT-CHATGPT-CALLBACK"],
    "resource": "https://mcp.gravitalia.com/law/mcp",
    "scopes": ["legal:read"]
  }
]
```

Copy the exact callback displayed when creating the ChatGPT connection. This is
a public OAuth client, with token endpoint authentication method `none`; it has
no shared client secret. Registration is deployment controlled, not anonymous.
The dashboard reconciles this policy with Hydra and refuses conflicting existing
registrations. The authorization bridge binds Hydra's audience to the registered
MCP resource. Tokens issued for the dashboard or another namespace are rejected.

Use an externally reachable `AUTH_PUBLIC_ORIGIN` and a consistent `AUTH_ISSUER`.
The OAuth metadata lives at `/.well-known/oauth-authorization-server`; protected
resource metadata lives under `mcp.gravitalia.com`. The existing Galadril identity,
passkey and consent checks remain in force. Enabling the Docker overlay does not
create a Cloudflare tunnel, DNS record or ChatGPT connection.

Native Pengolo calls do not reuse the public OAuth token or expose the generation
capability to the provider. The client exchanges generation authority with Gateway
for a random, resource-bound grant lasting 60 seconds. The MCP introspects this
grant on every call. Gateway checks the active generation, owner, current user
access, selected registered capability, execution context and resource. A user,
session or MCP identifier alone never authorizes an invocation. Grants cannot
invoke the other chat tools and expire after Gateway restarts.

## Deployment

The root CI publication target includes `//services/mcp:push`.
For a local image, build and load `//services/mcp:load_local`.

Build `//services/mcp:images`. The normal Compose configuration includes the
read-only unprivileged MCP service through `infrastructure/docker/mcp.yaml`.
It exposes the workspace resource at `/workspace/mcp` and the optional legal
provider at `/law/mcp`. The existing API Envoy process owns the
loopback MCP listener on port 8098, with an exact host match for
`mcp.gravitalia.com`. No additional proxy or tunnel container is needed.

Add a public hostname to the existing Cloudflare Tunnel configuration:
`mcp.gravitalia.com` → `http://127.0.0.1:8098`, preserving the original Host
header (or explicitly setting it to `mcp.gravitalia.com`). The existing Compose
connector `utils-email-tunnel` shares the API proxy network namespace. Its
`tunnel` profile enables the connector independently of email tracking. An independently managed
connector can be reused if it can reach this loopback listener. Native Pengolo
calls do not require Cloudflare. No host port is published for MCP, and the
listener returns 404 for hosts or paths outside its public contract.

The managed development bootstrap mounts each available PISTE credential file
independently. Neither file is required for MCP startup. Without usable legal
credentials, legal tools return `status: unavailable`, without verifying any
source, and callers can continue with authorized Web research. When
`.local/cloudflared-token` exists, it enables the existing connector and mounts
that file without copying its contents into configuration or arguments.

For public Galadril login, set `GALADRIL_DEV_PUBLIC_ORIGIN` in the managed
owner-only `.local/dev/compose.env`, then regenerate with the development CLI.
The bootstrap adds an exact-host HTTP listener at `127.0.0.1:8099` to the same
Envoy process. Point a second tunnel hostname to that listener. HTTPS terminates
at Cloudflare; this HTTP hop remains inside the shared loopback namespace. The
public issuer is consistent across Hydra, the dashboard and Envoy, while the
local diagnostic JWT issuer remains separate. Register the exact callback shown
by ChatGPT using `AUTH_MCP_CLIENTS`. Use a predefined public OAuth client with
PKCE S256, token endpoint authentication `none`, no client secret, and scope
`legal:read`. OAuth discovery is routed to the dashboard, not the GraphQL API.

`MCP_NAMESPACES` declares installed providers, capability identities, paths, OAuth
scopes and allowed clients. These are operator settings, never tool arguments.
The service validates each namespace independently and returns 404 for unknown
paths. Future providers use this registration contract and the shared verifier.

The workspace resource uses its own OAuth client and `workspace:use` scope,
plus granular mail, document and data scopes. Native Pengolo operations use the
same MCP transport with generation-scoped grants. External email sends require
review of the exact draft revision in a primary Galadril session; MCP annotations
are descriptive hints, not approval authority. See
[the service guide](../../../services/mcp/README.md) for tools, scopes,
private attachment handling and compilation workflow.

PISTE secrets are mounted only into the MCP service, never Scribe, the tunnel,
Envoy or a model prompt. Default file mounts are `.local/piste-key` and
`.local/piste-oauth-key`; they must be regular files with mode 0600, readable by
the configured service UID. The API key authenticates Judilibre. Légifrance uses
the OAuth client ID and secret. `SECRET_KEY` in the API-key file is not the OAuth
secret; use the separate OAuth file. JSON credential files are also supported.
Server deployments may instead provide `LAW_PISTE_API_KEY`,
`LAW_PISTE_CLIENT_ID`, `LAW_PISTE_CLIENT_SECRET` and
`LAW_PISTE_ENVIRONMENT=production|sandbox`. Never put these values in a plugin
manifest or repository.

The API adapters use fixed PISTE origins, bounded responses and timeouts, cached
OAuth tokens and no redirects. Search results are leads, not verified citations.
`verify_citation` re-fetches a source and separately checks identity and quotation;
it never certifies legal applicability. OTLP spans record provider operation and
authorization outcomes without prompts, document text, query bodies or tokens.

## Verification

Tests use synthetic credentials and mock official API responses. MCP transport
tests exercise unauthenticated denial, public metadata and authenticated discovery.
Gateway tests cover resource-bound delegation. A real external OAuth connection
still requires the configured public issuer, exact callback and Cloudflare tunnel.
