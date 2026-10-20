# ChatGPT plan usage in personal open-source installations

Galadril uses the documented Sign in with ChatGPT open-source flow, not Codex's
private backend or another application's OAuth client. This optional integration
consumes the user's shared Codex / ChatGPT Work allowance. It does not import
ChatGPT conversations or memories. Existing Galadril conversation persistence,
authorization, tools, local Python sandbox and document retrieval remain authoritative.

## Ownership and deployment

Gateway derives an opaque account owner from the authenticated tenant and user;
clients cannot choose another owner. Scribe owns encrypted provider credentials,
which never enter prompts, conversation records, browser storage or telemetry.
Its protected SQLite store is local to one personal installation and requires a
separate encryption key and persistent volume. It is not a shared SaaS credential
broker. Pending OAuth attempts expire after five minutes and are capped globally.
The key and volume must be backed up together; key rotation requires reconnecting.

Both Gateway and Scribe disable the integration by default through environment
configuration. Gateway's optional callback listener is separate from its API and
uses a fixed `http://127.0.0.1:<port>/auth/callback` redirect. Docker publishes this
port on the host loopback only. Kubernetes does not publish it through ingress:
personal deployments use a local port forward. The browser must reach the same
personal installation through this loopback callback. A remotely hosted service
offered to multiple users requires OpenAI's separate approval.

## Local setup and user settings

The default is API-only. Keep the existing `SCRIBE_OPENAI_API_KEY` configured for
users who do not connect a plan and for quota fallback. OAuth does not replace
Galadril login or require an administrator role.

For Docker, set `GALADRIL_CHATGPT_ENABLED=true` in the deployment environment and
set `SCRIBE_CHATGPT_ENCRYPTION_KEY` to a persistent base64-encoded 32-byte random
key (`openssl rand -base64 32`). The development environment helper creates this
key automatically. Recreate Gateway, Scribe and the API proxy after configuration
changes. Keep the key private and retain the `scribe-chatgpt` volume. The optional
`GALADRIL_CHATGPT_CALLBACK_PORT` defaults to 1455 and must be free on the local host.

For a personal Kubernetes installation, create a `chatgpt-runtime` Secret in the
`galadril` namespace with an `encryption-key` entry holding the same base64-encoded
32-byte key. Apply `infrastructure/overlays/chatgpt-personal` instead of
the base kustomization. This overlay enables both services, adds persistent local
credential storage and keeps a single Scribe replica. Before signing in, run
`kubectl -n galadril port-forward --address 127.0.0.1 deployment/gateway 1455:1455`
on the browser's computer. No Service or ingress publishes the callback port.

Open the account menu, choose **User settings**, then **Continue with ChatGPT**.
Complete sign-in in the provider window and return to Galadril. Settings display
the selected account, usage controls and a disconnect action. When the deployment
flag is disabled, settings explain that conversations use the configured API key.
Disconnecting removes local credentials; a failed remote revocation is explicitly
reported so the user can also revoke Galadril from ChatGPT settings.

## Authorization lifecycle

Authenticated account management starts an authorization-code flow with fresh
PKCE S256, state and nonce. A stable host ID identifies the installation. First
registration uses `dynamic_agent_client`; subsequent sign-ins reuse the issued
client ID and validated account identity. The callback is single-use, validates
the ID token's signature, issuer, audience, expiry and nonce, and checks the
granted `chatgpt.tokens.use.direct` scope. Missing permission disables plan usage.
Multiple account registrations remain separate, including registrations with the
same email. The active registration is explicit. Disconnect attempts revocation
and removes local tokens even when remote revocation cannot be confirmed.

Refreshes are serialized for each renewable session. Replacements are persisted
before use. Revoked or invalid renewable sessions require reconnection. Catalogs
are fetched with the active access token and intersected with the administrator's
configured models; a catalog is not proof that inference will succeed.

## Model requests and fallback

PydanticAI continues to drive the agent loop. The plan transport uses only the
public Responses API, `store=false`, `stream=true`, full required input history,
developer instructions and function declarations in `additional_tools` input items. It omits fields rejected
by the preview route, including `max_output_tokens` and `previous_response_id`.
Galadril executes MCP, Skills and Python tools itself; hosted MCP, Code Interpreter,
image generation, file search and native tool search are not available on this
route. Unsupported inputs are reported rather than represented as supported.

With configured API credentials, a quota exhaustion can switch subsequent model
requests to the API. The UI identifies the billing source and the fallback. Retry
occurs at the model-request boundary, retaining completed tool results and never
restarting the conversation or replaying tools. Partial failed model output must
not become a successful response. Authorization/policy failures are not treated
as quota exhaustion. API failures cannot trigger an unlimited retry loop.

OTLP counters record bounded outcome/provider labels only. Tokens, authorization
URLs, account emails, prompts and raw upstream errors are excluded.

## Official contracts

- [Registration](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [Accounts and renewal](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
- [Inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [Preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
- [Error recovery](https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery)
- [User usage controls](https://learn.chatgpt.com/docs/sign-in-with-chatgpt)

This document defines the implementation contract; completion and validation are
recorded in the associated pull request.
