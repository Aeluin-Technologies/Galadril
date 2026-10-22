# Dashboard browser tests

Run the compiled Nuxt application in Chromium with Bazel:

```bash
bazel test //tests/e2e:dashboard_browser_test --config=ci --config=release
```

Use the existing BuildBuddy authentication configuration. The target needs a
Linux Docker executor with network access and is included automatically in
`bazel test //...` and the existing CI workflow. It does not need a locally
running dashboard, demo credentials, or a developer's passkey.

The fixture builds the production Nuxt application and starts PostgreSQL,
Kratos, Polis, Hydra and Envoy. It creates isolated password accounts and an
OIDC test identity provider. Chromium uses its native virtual WebAuthn
authenticator for enrollment and subsequent verification, including user
verification. The generated fixture CA is trusted explicitly; HTTPS validation
and the application's security headers remain enabled.

A preflight first checks that Chromium rejects the untrusted fixture certificate.
Only then is its public CA imported into the container's NSS trust store and the
browser suite started. The CA expires after one day, is regenerated each run and
is never installed on the host machine. No developer key or certificate is copied
to a remote executor. The browser origin uses `localhost`, since WebAuthn RP IDs
must be domain names rather than IP addresses.

Browser scenarios cover anonymous route protection, email/password transitions
without a document reload, rejected passwords, mandatory passkeys, enterprise
OIDC sign-in, secure HttpOnly cookies, session reload, logout and administrator
route protection. Translations must load before sign-in and remain available in
the dashboard and onboarding tour. Administration scenarios cover the organization landing page,
self-suspension prevention, confirmation before suspension or passkey reset,
email domain normalization, explicit acceptance of SSO discovery, permission
import opt-in, visual/code round trips and narrow-screen scrolling.

Guided administration scenarios additionally create and assign roles, inspect
the access graph, review and save UTC access rules, and surface safe actionable
errors. Enabling mandatory passkeys opens a native modal, checks the WebAuthn
Permissions-Policy header and verifies the existing key without replacing the
document. People refresh uses an animated icon. Active-session checks exercise
same-origin renewal, encrypted HttpOnly refresh state, a temporary outage and
recovery while retaining the current page. Unit tests cover idle/absolute expiry,
identity substitution and credential-reset recovery separately.

Administration data comes from a test-only GraphQL double behind Envoy's actual
JWT verification. These assertions verify the frontend's behavior, not Gateway,
Cedar or SpiceDB enforcement. The Gateway and pipeline lifecycle tests exercise
those real authorization services separately. SSO provider discovery in these
browser tests uses deterministic responses; its network security checks have
dedicated Gateway tests.

Identity and passkey persistence use Gateway's production Rust private GraphQL
adapter against PostgreSQL, including its RLS and atomic updates. Nuxt receives
only the adapter URL and a disposable service credential, never a database URL.
Every scenario also rejects telemetry requests from browser code.

Playwright and its browser image are pinned together. Tests run once without
retries. Failure screenshots, traces and JUnit results are saved under Bazel's
undeclared outputs (`results/`). These artifacts contain only disposable test
accounts, but traces may include their credentials and tokens. No developer
browser profile or session is used.

Scenarios share the executor's source IP. Before starting another sign-in, the
suite reserves its request budget and, when necessary, waits until the server's
`X-RateLimit-Reset` deadline. The observed budget is saved in the run output
directory so a worker restart after a failed scenario cannot reset it. Production limits stay unchanged. An unexpected 429
still fails the scenario; refused requests are never retried.
