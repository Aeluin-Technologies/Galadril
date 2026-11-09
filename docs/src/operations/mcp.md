# Authenticated MCP hub

The public MCP at `https://mcp.gravitalia.com/` combines workspace and legal
tools. Légifrance and LibreJustice are searched in parallel. Citation checks
fetch directly from Légifrance.

## Authority

Public clients use the Galadril login and OAuth authorization code with PKCE
S256. Configure `AUTH_MCP_CLIENTS` on the dashboard with a JSON array:

```json
[
  {
    "id": "galadril-mcp-chatgpt",
    "name": "Galadril",
    "redirectUris": ["https://REPLACE-WITH-THE-EXACT-CHATGPT-CALLBACK"],
    "resource": "https://mcp.gravitalia.com/",
    "scopes": ["workspace:use", "legal:read", "email:read", "email:write"]
  }
]
```

Copy the exact callback displayed when creating the ChatGPT connection and
enter the predefined client ID manually. This is
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
The root URL exposes the combined catalog. The former `/workspace/mcp` and
`/law/mcp` URLs keep their own OAuth audiences for existing integrations.
The existing API Envoy process owns the
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

Run `uv run infrastructure/dev/galadril.py mcp-keys` to select LibreJustice,
PISTE OAuth or the PISTE API key. Prompts hide input and preserve existing files
unless replacement is confirmed. The next `uv run infrastructure/dev/galadril.py up`
mounts the available private files into MCP and refreshes the mounts after rotation.
Each file is optional. Without usable legal credentials, legal tools report their
availability and callers can continue with authorized Web research. When
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
PKCE S256, token endpoint authentication `none`, no client secret, and the
scopes listed above. OAuth discovery is routed to the dashboard.

`MCP_NAMESPACES` declares installed providers, capability identities, paths, OAuth
scopes and allowed clients. These are operator settings, never tool arguments.
The service validates each namespace independently and returns 404 for unknown
paths. Future providers use this registration contract and the shared verifier.

The root resource requires `workspace:use` and a scope for each tool family.
Add `documents:read`, `documents:write` or `data:read` for those tools.
Native Pengolo operations use the
same MCP transport with generation-scoped grants. External email sends require
review of the exact draft revision in a primary Galadril session; MCP annotations
are descriptive hints, not approval authority. See
[the service guide](../../../services/mcp/README.md) for tools, scopes,
private attachment handling and compilation workflow.

PISTE secrets are mounted only into the MCP service, never Scribe, the tunnel,
Envoy or a model prompt. Default file mounts are `.local/piste-key` and
`.local/piste-oauth-key`; they must be regular files with mode 0600, readable by
the configured service UID. The API key authenticates Judilibre. When it is
missing, Judilibre calls return `status: unavailable` without a network request.
Légifrance uses
the OAuth client ID and secret. `SECRET_KEY` in the API-key file is not the OAuth
secret; use the separate OAuth file. JSON credential files are also supported.
Server deployments may instead provide `LAW_PISTE_API_KEY`,
`LAW_PISTE_CLIENT_ID`, `LAW_PISTE_CLIENT_SECRET` and
`LAW_PISTE_ENVIRONMENT=production|sandbox`. Never put these values in a plugin
manifest or repository.

The development CLI stores the LibreJustice token in `.local/librejustice-key`.
Other deployments can mount a mode-0600 token file and set
`LIBREJUSTICE_API_KEY_FILE` to its absolute path in the container, or inject
`LIBREJUSTICE_API_KEY` via the service environment. A missing key, timeout or
rate limit leaves Légifrance search available. If Légifrance is unavailable,
LibreJustice can still return leads for later verification.

Mount credential files directly in `/run/secrets`. For a custom mount
directory, set `MCP_CREDENTIALS_DIR` in the MCP service environment.

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
