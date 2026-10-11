# Enterprise authentication

## Tenant-owned administration contract

Gateway manages verified email domains, SSO connections, identity enrollment
and permission import settings. Administrative inputs never select another
tenant: Gateway derives the organization from the authenticated principal.
Tenant administrators and authorized automation use the same authorization and
audit boundary. Permission imports are disabled by default and executed by
Gateway, not Studio.

A trusted deployment provisioning process initializes an organization and its
first administrator through an API, without SQL or manual tenant identifiers.
Its credential cannot update existing organizations. Subsequent management
uses tenant administrator permissions. Additional email domains require DNS
TXT ownership verification. Unknown email domains never cause anonymous
organization creation.

Polis owns SSO secrets and encrypts PostgreSQL records with its native
AES-256-GCM store. Gateway stores only connection references and routing
metadata. Secrets are write-only inputs and never appear in management queries
or audit details. Hydra owns signing keys and OAuth client credential hashes.
OIDC discovery and remote JWKS replace copied public keys. Upstream client
registration depends on the enterprise IdP; confidential-client credentials
can be entered securely through Gateway when registration is unavailable.

This contract separates enterprise identity federation from Galadril resource
authorization. Polis handles upstream SAML2 and OIDC connections. Hydra is the
only issuer of Galadril API access tokens. Studio's server implements the login
and consent bridge; its browser code never receives an OAuth client secret or a
refresh token. Gateway and Registry receive no issuer signing keys.

Studio has no database driver, database credential, SQL, or migration ownership.
Its server retains HTTP cookies, CSRF checks and OAuth navigation. Canonical
identity resolution and all passkey persistence go through Gateway's private
GraphQL schema at `/internal/auth/graphql`. This schema uses a separate service
credential, is absent from public ingress, and preserves transaction-local RLS,
one-time challenges, counter preconditions and reset generations in Gateway.

Nuxt's framework telemetry is disabled. Browser code exports no telemetry.
Server metric export is disabled by default and can export only aggregated
authentication counts with bounded operation/outcome labels. It exports no
traces, logs, request bodies, identities, tenant identifiers or host/process
resource attributes.

## Identity and tenant binding

An administrator registers an exact, verified email domain against a Galadril
tenant and a Polis connection. Domains select a connection; they never prove
identity or membership. A domain has exactly one owner and cannot silently choose
between tenants. Connection administration is private and separate from the
unauthenticated discovery endpoint.

The authenticated principal is identified by the tuple (tenant, connection,
external subject). Email addresses are mutable display attributes and must not
link accounts. The callback verifies the selected connection's OAuth transaction
before resolving this tuple to an immutable Galadril user ID. Disabled or deleted
users cannot authenticate or be recreated by just-in-time provisioning.
Provisioning requires an explicitly enabled connection policy and grants no
SpiceDB relationships or Cedar permissions.

Hydra has no user directory. External accounts map to `iam_users`; several
administrator-approved identities may reference one existing account without
duplicating its roles. Permission import is a separate, default-disabled tenant
option. A bounded manifest goes through Gateway's existing administrative
mutations and audit, using the caller's verified JWT. Only compatible Cedar
policies, existing role assignments, and supported data-domain relationships
are imported. Untrusted SSO claims cannot enable imports or grant permissions.

The optional local Compose profile uses Kratos for test credentials. Its immutable
identity ID binds explicitly to an existing IAM account; administrator-only Kratos
metadata carries canonical references, which Studio checks against the tenant's
active IAM binding. Public session responses and editable traits cannot establish
that binding. Gateway's deployment-authenticated local enrollment creates the
first test administrator automatically. Public registration remains disabled.
Local accounts use the same Hydra issuer and authorization boundary as enterprise
accounts, and logout revokes their Kratos session as well as Galadril cookies.

## Tokens and sessions

### Optional passkey second factor

Each organization can require a Galadril WebAuthn second factor after either
Polis federation or Kratos password authentication. The policy is disabled by
default; authentication fixtures explicitly enable it. No Hydra login grant or
Galadril access token is issued until the second factor succeeds. An account
without a credential enrolls one during this authenticated transaction. Passkeys
reference the existing canonical IAM user; this introduces no user directory.

Registration and assertions require user verification, the configured HTTPS
origin and its RP ID. Short-lived challenges are bound to the browser transaction,
tenant, user and reset generation and consumed once, including failed attempts.
Only public credential material is persisted under tenant RLS. Concurrent
enrollment, challenge replay and reset races fail closed.

Gateway exposes tenant-authorized, audited policy and credential reset mutations.
A reset deletes public credentials, invalidates pending challenges and advances
the user's generation atomically. The next successful primary authentication
requires enrollment again. Credentials cannot be reset through email recovery or
an unauthenticated endpoint. Gateway and Studio reject tokens whose second-factor
generation no longer satisfies an enabled policy.

Hydra issues short-lived asymmetric JWT access tokens with issuer, audience
`galadril`, Galadril `sub`, `tenant_id`, `iat`, `nbf` and `exp`. Optional `role`,
`region` and `device_trust` claims require a separate trusted mapping; arbitrary
upstream groups and request parameters cannot set them. ID tokens are never
accepted as API credentials. The first-party dashboard uses authorization code
with S256 PKCE, state and nonce. Refresh grants are initially disabled so tenant
and account status are rechecked at each new login.

The access token cookie is host-only, Secure, HttpOnly, SameSite=Lax and scoped
to `/`. Studio verifies its signature, issuer, audience, token purpose, expiry
and tenant binding before serving authenticated routes. Unsafe cookie-authenticated
requests require the exact public Origin; WebSocket upgrades enforce Origin as
well. Login transactions are bound to a short-lived encrypted browser cookie.
Logout clears both token and transaction cookies and prevents Hydra session reuse.

## Proxy and workload trust

The public Envoy exposes the dashboard, required Polis callback endpoints, Hydra's
authorization/discovery endpoints and authenticated `/graphql`. Provider admin,
introspection and database endpoints are never publicly routed. Initial enrollment
uses a dedicated credential on `/provisioning/organizations`; tenant management
uses the authenticated Gateway API.
Identity headers are removed before JWT validation. Envoy projects only verified
claims and strips credentials before forwarding to Gateway.
Expiry is enforced after signature verification using the projected `exp` claim;
Envoy's default clock tolerance cannot extend the token's API lifetime.

JWKS locations are deployment configuration, never JWT `jku` or browser input.
Remote keys use an authenticated TLS upstream (Compose) or authenticated Ambient
workload transport (Kubernetes). Public discovery advertises the canonical issuer
and HTTPS JWKS URI. Fetches have bounded timeouts and caches; no local fallback
accepts an unverified key. Rotation retains old verification keys until all issued
tokens expire. Gateway retains its active-user, SpiceDB, Cedar and RLS checks.
Registry retains its workload/RPC allowlists and receives no public login route.

Authentication emits bounded OTLP events and counters. Never record emails,
authorization codes, cookies, tokens, client secrets, or callback query strings.

## Enterprise protocols

Polis directly brokers OIDC and SAML2. LDAP and Kerberos are supported through an
enterprise IdP exposing OIDC/SAML; Polis is not a native LDAP or Kerberos server.
An optional LDAP-to-OIDC connector can be deployed separately when the enterprise
has no existing IdP. Galadril must not collect enterprise passwords.
