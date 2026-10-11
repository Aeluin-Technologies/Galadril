# Enterprise SSO

Galadril uses the free self-hosted Polis/Jackson and Hydra editions. No paid
Polis portal or Ory Network subscription is required. Polis brokers OIDC and
SAML2 and encrypts its PostgreSQL records with native AES-256-GCM. Hydra signs
Galadril JWTs and stores clients and sessions, not users. `iam_users` remains
the authoritative Galadril directory.

Organizations administer federation and permission imports through Gateway's
GraphQL API and the dashboard at `/settings/sso`. These operations derive the
tenant from the authenticated JWT. Neither administrators nor automation pass
a tenant identifier or edit routing tables manually.

## Deployment secrets

Compose includes `auth.yaml` from `infrastructure/docker/docker-compose.yaml`.
Build and publish the updated Gateway and Studio images before starting the
stack; existing `latest` images do not contain the new APIs and server routes.
Set independent random values in an untracked environment file:

```dotenv
AUTH_PUBLIC_ORIGIN=https://localhost:8080
AUTH_CLIENT_SECRET=<random-64-hex-characters>
AUTH_SESSION_SECRET=<random-64-hex-characters>
AUTH_GATEWAY_KEY=<different-random-64-hex-characters>
GALADRIL_AUTH_PASSWORD=<random-64-hex-characters>
HYDRA_DATABASE_PASSWORD=<random-64-hex-characters>
HYDRA_SYSTEM_SECRET=<random-64-hex-characters>
POLIS_DATABASE_PASSWORD=<random-64-hex-characters>
POLIS_ENCRYPTION_KEY=<random-32-hex-characters>
POLIS_API_KEY=<random-64-hex-characters>
POLIS_CLIENT_SECRET_VERIFIER=<random-64-hex-characters>
SSO_PROVISIONING_KEY=<random-64-hex-characters>
```

Generate each value independently with `openssl rand -hex 32`, except
`POLIS_ENCRYPTION_KEY` which uses `-hex 16`. Gateway and Studio share the private
Polis API key, not the enterprise client credentials. Only the trusted initial
provisioning process receives `SSO_PROVISIONING_KEY`; tenant administrators
never need this deployment credential. Keep the Polis encryption key in your
secret manager, separately from PostgreSQL backups.

Generate development certificates with
`infrastructure/envoy/dev-certificates.sh` in a fresh directory and set
`GALADRIL_IDENTITY_DIR`. Explicitly trust the development CA in the local browser;
production requires a trusted ingress certificate. Never disable TLS checks.
The canonical issuer is `https://aeluin.gravitalia.com`; change it consistently
in Hydra, Studio and Envoy when using another issuer. Public dashboard and
GraphQL URLs must share an HTTPS origin.

The development certificate covers `localhost`, `127.0.0.1` and workload names;
trusting it on one workstation does not make it trusted on another. A developer
running their own local stack must generate a fresh development CA and explicitly
trust its public `ca.pem` in their browser. Never distribute `ca-key.pem` or
workload private keys to testers.

For a shared remote demonstration, use a stable DNS hostname and a certificate
trusted by the testers' browsers. Set `AUTH_PUBLIC_ORIGIN` to the exact HTTPS
origin and `GALADRIL_PUBLIC_TLS_DIR` to an absolute server-side directory containing
`cert.pem` (the server certificate and intermediate chain) and `key.pem`. Compose
already mounts those files read-only into the public Envoy listener. Keep the
internal workload CA separate. Configure the same external origin in the SSO
provider's callback URLs; issuer changes must still be applied consistently as
described above. Certificates must be renewed by the deployment's certificate
manager and reloaded by the serving proxy.

Choose the hostname before enrolling test passkeys: Galadril derives their RP ID
from that hostname. A key enrolled for `localhost` cannot authenticate a different
demo hostname. An administrator can reset the affected account's passkey after
verifying its owner, then the next sign-in enrolls a key for the new hostname.
See the [WebAuthn RP ID requirements](https://www.w3.org/TR/webauthn-3/#rp-id).

Automated browser tests have a separate lifecycle: every run creates its own
short-lived CA, trusts only its public certificate inside the disposable Chromium
container and removes the container afterwards. No host trust-store change or
developer certificate is required. See `tests/e2e/DASHBOARD.md` for the browser
test command and coverage.

PostgreSQL initialization creates separate Hydra, Polis and login database
roles. On an existing volume, run
`infrastructure/docker/init-scripts/03-init-auth.sh` with its required environment
before applying Gateway migrations. If the auth role was absent during the
migration, grant it schema usage, SELECT on `sso_connections` and SELECT/INSERT
on `iam_users` and `iam_external_identities`. Never grant BYPASSRLS or permission
engine credentials to Studio. The self-service migration removes the old
plaintext broker-secret column; rotate previously copied secrets if upgrading
an experimental deployment that used it.

For Kubernetes, install `infrastructure/kubernetes/istio/istiod.yaml`, including
`PILOT_JWT_ENABLE_REMOTE_JWKS=envoy`. Provision PostgreSQL with validated TLS
and these Secrets using your secret manager:

| Secret                | Keys                                                                                    |
| --------------------- | --------------------------------------------------------------------------------------- |
| `hydra-runtime`       | `DSN`, `SECRETS_SYSTEM`                                                                 |
| `polis-runtime`       | `DB_URL`, `DB_ENCRYPTION_KEY`, `JACKSON_API_KEYS`, `CLIENT_SECRET_VERIFIER`             |
| `studio-auth-runtime` | `AUTH_CLIENT_SECRET`, `AUTH_SESSION_SECRET`                                             |
| `gateway-auth-client` | `AUTH_GATEWAY_KEY`                                                                      |
| `gateway-runtime`     | existing Gateway keys, `AUTH_DATABASE_URL`, `SSO_POLIS_API_KEY`, `SSO_PROVISIONING_KEY` |

Use one `JACKSON_API_KEYS` value equal to `SSO_POLIS_API_KEY`; Studio references
this same Secret key. Only Gateway receives `AUTH_DATABASE_URL`, targeting its
database as `galadril_auth`; Studio uses its private GraphQL API and receives
no PostgreSQL credentials. `AUTH_GATEWAY_KEY` is shared only by Gateway and
Studio, must have at least 32 random characters, and must differ from the
provisioning credential. Public Envoy and waypoint routes do not expose
`/internal/auth/graphql`; the auth workload's mesh identity may call that exact
path. Hydra and Polis use separate databases. Set the canonical
origin consistently in `auth.yaml`, `gateway.yaml` and `ingress.yaml`, including
the browser Origin check. Apply Gateway migrations with the migration owner.
The auth pod keeps Hydra's admin API on loopback; native mesh transport and
waypoint policy restrict Polis management to Gateway. Public routes expose
neither provider administration nor Hydra's admin API.

Authentication service endpoints (`AUTH_GATEWAY_URL`, `AUTH_HYDRA_ADMIN_URL`,
`AUTH_POLIS_URL` and the Kratos URLs) are trusted, server-only deployment
configuration. Tenant input and browser parameters never select these service
origins. Private Compose connections use HTTP inside their network; Kubernetes
uses the mesh transport policy. Redirects are refused when forwarding service
credentials. Requiring HTTPS for these loopback endpoints would break these
deployment profiles without securing a user-controlled destination.

The browser test's `TEST_UNDECLARED_OUTPUTS_DIR` and `XML_OUTPUT_FILE` are
absolute paths supplied by the Bazel runner, not application input. Keep them
intact so Bazel can collect artifacts and JUnit results. Confining them below the
fixture's temporary directory would silently discard the runner's outputs.

Nuxt telemetry and its OTLP export are disabled by default. To enable aggregate
server counters, set `NUXT_AGGREGATE_METRICS_ENABLED=true` and
`NUXT_METRICS_OTLP_ENDPOINT` to the trusted private collector's `/v1/metrics`
URL. These are server-only settings. The exporter allows only authentication
counts grouped by the fixed operation and outcome labels, drops other
instruments and attributes, disables exemplars, and has no trace or log exporter.

## Automatic organization provisioning

A trusted signup, billing or enterprise onboarding process verifies initial
domain ownership and the first administrator's immutable IdP subject. It calls
`POST /provisioning/organizations` over HTTPS with the deployment credential
in `X-Galadril-Provisioning-Key`:

```json
{
  "request_id": "c169b727-0fb4-4396-a846-a84069ef8162",
  "owner_external_subject": "<verified-immutable-idp-subject>",
  "connection": {
    "domain": "acme.example",
    "oidc_issuer": "https://login.acme.example",
    "oidc_client_id": "<enterprise-client>",
    "oidc_client_secret": "<enterprise-secret>"
  }
}
```

Gateway generates and reserves stable organization and owner IDs, initializes
Registry storage, configures Polis, creates the canonical IAM account and SSO
binding, writes its SpiceDB administrator relationship, then publishes email
discovery. Retry the same request UUID after a transient failure. Replays retain
the same IDs and cannot modify an already provisioned organization. Domains
cannot be reassigned to another organization. The bootstrap subject must come
from a verified trusted process; this API is deliberately unavailable to
anonymous email discovery. Tenant administration then uses ordinary Galadril
administrator credentials.

## Organization administration

Sign in, open your avatar menu and choose **Administration**. The landing page
contains the organization name and links to **People**, **Sign-in & security**,
and **Import access rules**. The same destinations are available in the command
bar to authorized administrators. People shows canonical accounts with their
provider display metadata; email is never used to merge identities. Suspending
a person retains their data and blocks subsequent authenticated HTTP requests.
Role changes and account recovery require an explicit confirmation.
Gateway refuses self-suspension and self-deletion, including attempts through
the user upsert API. User state changes serialize per organization and recheck
the active administrator inside the transaction. Since tenant management
requires a direct SpiceDB administrator relationship, the surviving caller
preserves administrative access even during concurrent mutual suspensions.
Deletion disables the target before removing its authorization relationships.

To add a domain, follow **Your domain → Your provider → Review**: request its challenge,
publish the returned `_galadril.<domain>` DNS TXT record and save the provider.
Challenges expire after 24 hours. Gateway verifies the proof over authenticated
HTTPS before reserving the domain or configuring Polis. Existing domains can
rotate credentials without repeating DNS verification. Removing or replacing
an issuer requires an explicit identity migration; updates preserve Polis IDs.

In **Your provider**, enter an HTTPS provider address or SAML metadata URL and
choose **Detect configuration**. Gateway validates OIDC discovery or SAML2
metadata and checks the domain proof. The preview reports pending ownership
verification separately; detection never reserves a domain or saves a connection.
Choose **Use detected settings** to prefill the form, then review and save it.
Client secrets remain write-only and cannot be discovered from public metadata.
Discovery rejects private network addresses, follows no redirects, pins DNS
answers, verifies TLS and limits response size and request duration.

OIDC configuration requires only the issuer and verified domain when the IdP
supports Dynamic Client Registration. Gateway discovers
`/.well-known/openid-configuration`, validates the exact issuer and public HTTPS
endpoints, and optionally sends a write-only `oidc_registration_token` to the
advertised registration endpoint. The registered callback is
`https://aeluin.gravitalia.com/api/oauth/oidc`. If registration is unavailable,
supply both `oidc_client_id` and `oidc_client_secret`. Empty credentials on an
existing connection reuse its encrypted credentials. Discovery refreshes the
upstream JWKS without manually copying public keys. The initial registration
token is not retained. Enterprise client IDs cannot be rotated into a different
Polis identity namespace through an ordinary credential update.

For SAML2, submit trusted `saml_metadata` XML instead of OIDC fields. The ACS is
`https://aeluin.gravitalia.com/api/oauth/saml` and the audience is
`https://aeluin.gravitalia.com/sso`. Signed metadata/certificate rotation is an
administrator operation; OIDC `.well-known` does not replace SAML metadata.
LDAP and Kerberos remain behind the enterprise IdP exposing OIDC or SAML2;
Galadril never collects enterprise passwords.

Gateway exposes these GraphQL operations to both UI and automation:

- `ssoSettings`: secret-free settings for the current organization.
- `updateOrganization(name)`: changes its display name, preserving its identity.
- `users`, `roles`, `roleAssignments`: authorized member and access listings.
- `updateUser(userId, isActive)`: suspends or restores an existing account.
- `setPasskeyMfa(enabled)`: changes the default-disabled MFA requirement.
- `resetUserPasskeys(userId)`: invalidates a member's key and pending challenges.
- `requestSsoDomain(domain)`: returns a DNS ownership challenge.
- `detectSsoProvider(domain, address)`: previews validated provider metadata.
- `configureSsoConnection(configuration)`: creates or updates federation.
- `disableSsoConnection(domain)`: stops new logins while retaining identity links.
- `linkExternalIdentity(domain, externalSubject, userId)`: binds an immutable IdP
  subject to an existing active Galadril account without duplicating its roles.
- `setPermissionImport(enabled)` and `importPermissions(manifest)`.

All require an active administrator and pass SpiceDB/Cedar authorization and
append-only audit. The existing GraphQL `JSON` scalar carries a JSON-encoded
string, including for settings and result values. Example request:

```json
{
  "query": "mutation($configuration: JSON!) { configureSsoConnection(configuration: $configuration) }",
  "variables": {
    "configuration": "{\"domain\":\"acme.example\",\"oidc_issuer\":\"https://login.acme.example\"}"
  }
}
```

Send requests to `/graphql` using the HttpOnly session cookie and exact public
`Origin`, or a valid Galadril access token in `Authorization: Bearer ...`.
Secrets are write-only, cleared from dashboard fields after submission, and
stored only inside Polis's encrypted database records. Gateway's routing
metadata and settings never contain enterprise secrets.

JIT account creation is opt-in (`jit_provision`, false by default). Prefer
explicit account links for existing users. Identity keys are the tenant,
connection and immutable upstream subject; email never merges accounts.
Repeated and concurrent logins reuse the same account; disabled or deleted
accounts cannot be recreated. New JIT accounts receive no permissions merely
by signing in.

## Optional permission import

The organization's administrator enables imports through
`setPermissionImport(enabled: true)`. Automation uses the same administrator
credential and authorization boundary. No global operator approval or SQL edit
is needed. Imports remain disabled by default for every new organization.

```json
{
  "version": 1,
  "operations": [
    {
      "kind": "role_assignment",
      "user": "alice",
      "role": "analyst",
      "enabled": true
    },
    {
      "kind": "relationship",
      "resource": { "type": "group", "id": "research" },
      "relation": "reader",
      "subject": { "type": "user", "id": "alice" },
      "enabled": true
    },
    {
      "kind": "cedar_policy",
      "id": "office-hours",
      "content": "forbid(principal, action, resource) when { context.hour_utc < 8 };",
      "active": true
    }
  ]
}
```

Pass this object as a JSON-encoded `manifest` to `importPermissions`. It contains
local Galadril IDs and no tenant selector. The enterprise exporter maps its
subjects and groups to existing Galadril users, roles and data-domain groups.
Supported relationships are `reader` and `ingester` between groups and users;
arbitrary SpiceDB schemas, usersets, cross-tenant identifiers and service
principals are rejected. Cedar policies must pass Gateway's native validator.
SSO claims never enable imports or grant roles implicitly.

Batches allow 64 operations, 128 KiB overall and 8 KiB per Cedar policy. Gateway
validates the full manifest and all Cedar syntax before writes, then executes
native IAM operations in order, with authorization and audit on each operation.
It stops at the first error or denial and returns `applied`, `failed_at` and
`complete`; earlier changes remain applied. This is not a transaction across
PostgreSQL and SpiceDB. Retry idempotent operations after correcting the failure.
Disabling imports does not revoke existing permissions; use `enabled: false`
or policy `active: false` for explicit revocation.

## Verification

Run `bazel test //services/gateway:gateway_test
//services/gateway:sso_policy_test //front/dashboard:auth_test
//infrastructure/auth/tests:sso_test //infrastructure/envoy/tests:proxy_test
//infrastructure/kubernetes:ambient_test`, then `bazel test //...`.
The SSO integration fixture uses real free Polis, Hydra, PostgreSQL, the built
Studio image and Envoy with verified TLS. Its deterministic enterprise IdP
exercises login, durable identity reuse and JWT projection. Gateway tests cover
administrative isolation and the default-disabled import boundary separately.

## Local test accounts

Use the optional `local-auth` Compose profile for demonstrations and tester
accounts. It runs the free Apache-licensed Kratos 26.2.0 image, pinned by digest.
Kratos stores password credentials in its separate PostgreSQL database. Hydra
continues to issue the same Galadril access tokens consumed by Envoy. Polis
continues to handle enterprise OIDC/SAML connections. GitHub login is not enabled.

Add the following to `infrastructure/docker/.env`, alongside the existing SSO
secrets. Generate independent values with `openssl rand -hex 32`; the cipher
secret must contain exactly 32 characters, so use `openssl rand -hex 16` for it.

```dotenv
AUTH_LOCAL_LOGIN_ENABLED=true
KRATOS_DATABASE_PASSWORD=<random database password>
KRATOS_COOKIE_SECRET=<random cookie secret>
KRATOS_CIPHER_SECRET=<random 32-character cipher secret>
```

For an existing development database, apply the idempotent identity database
initialization after recreating PostgreSQL with these environment values:

```sh
docker compose -f infrastructure/docker/docker-compose.yaml --profile local-auth up -d postgres
docker compose -f infrastructure/docker/docker-compose.yaml exec -T postgres /docker-entrypoint-initdb.d/030-init-auth.sh
docker compose -f infrastructure/docker/docker-compose.yaml --profile local-auth up -d
```

Export the deployment's `SSO_PROVISIONING_KEY` in your shell, then enroll a tester:

```sh
uv run infrastructure/docker/tools/local_accounts.py --email tester@example.com
```

The tool prompts for a password without echoing it, creates the Kratos identity,
and calls Gateway's authenticated `/provisioning/local-organizations` endpoint.
Gateway generates the organization and owner IDs, initializes Registry storage,
and grants the owner tenant administration through the existing authorization
service. Each invocation for a new email creates an isolated test workspace.
No tenant ID or SQL intervention is required. Enrollment state is stored under
`.local/auth/accounts` with owner-only permissions; preserve it for idempotent
retries. Do not delete that state to reset an account. Passwords are never stored
in Gateway, in this state file, or in command arguments.

Open `/login` and enter the account's email. Registered enterprise domains go
directly to their SSO provider; when local login is explicitly enabled, other
addresses continue through Kratos. There is no separate test-account button.
Kratos's browser flow retains its CSRF protection. The BFF requires fresh authentication,
reads administrator-owned references through the private Kratos API, and checks
the explicit immutable identity binding against the active IAM account under
tenant RLS. User-editable traits and email addresses cannot select a tenant or
grant permissions. Logout revokes the Kratos browser session and clears the
Galadril cookie. Public registration and administrative identity APIs are blocked
by Envoy; login submissions are rate limited.

Both the local login option and local organization enrollment are disabled unless
`AUTH_LOCAL_LOGIN_ENABLED=true`. The profile is intended for local Compose; the
production Kubernetes deployment retains enterprise SSO without Kratos.

The lifecycle E2E environment starts Kratos, Hydra, Studio and Envoy, provisions
fixture credentials, and obtains each actor's JWT through the browser login flow
before that actor performs a business operation. It never signs application JWTs.
Cross-tenant negative cases use separately authenticated accounts; expiration is
tested using a real Hydra token with a short client-specific lifetime. Fixture
logins are paced to respect the deployed authentication rate limit. Only the E2E
Hydra default access-token lifetime is extended to cover the bounded lifecycle
scenario; the shipped deployment retains its 15-minute lifetime.

Run authentication independently of the ML lifecycle:

```sh
bazel test //tests/e2e:authentication_e2e_test //infrastructure/auth/tests:sso_test
bazel test //tests/e2e:pipeline_lifecycle_test
```

The standalone authentication test runs native Kratos, Hydra, TLS and Envoy, with
an echo upstream to inspect verified claim projection. The lifecycle test uses
the actual Gateway, Registry, ingestion and ML services.

## Passkey second factor

An organization can require a passkey after either enterprise federation or a
local Kratos password. This setting is off by default. Integration and lifecycle
test fixtures explicitly enable it and use a software authenticator with real
ES256 signatures; there is no bypass or test-account button in the dashboard.
Hydra receives the completed login only after verification. Its access JWT
contains `acr=urn:galadril:webauthn:uv`, `mfa_generation` and authentication
methods. Galadril keeps the canonical IAM user; no extra user directory is
introduced for WebAuthn.

SimpleWebAuthn validates the challenge, exact HTTPS origin, relying-party ID,
signature, counter and required user verification. The relying-party ID comes
from the hostname of `AUTH_PUBLIC_ORIGIN`, so configure that origin consistently
and keep it stable across deployments. The first verified primary login enrolls
one passkey if none exists; subsequent logins require that credential. Biometrics
and device PINs remain on the authenticator. There is no password-only fallback
when MFA is required.

`resetUserPasskeys` is tenant-bound, authorized and audited. Verify the person's
identity using your organization's recovery procedure before invoking it. The
transaction increments their credential generation, removes the old key and
invalidates pending challenges. Old JWT generations are rejected by Studio and
Gateway; a fresh primary login then enrolls a replacement. HTTP requests and new
WebSocket upgrades recheck account status and current MFA state. Existing
WebSocket connections close at token expiry. Resetting another tenant's key is
not permitted.

Permission imports remain off by default. **Visual blocks** supports role
assignments, compatible data-space relationships and advanced Cedar policies;
**Code** accepts the same versioned manifest. Unsupported code is never silently
converted or discarded. Review the requested changes before confirming. Imports
are ordered and can partially succeed: an error reports how many earlier changes
were retained.
