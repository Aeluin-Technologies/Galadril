# Document signatures

Galadril can sign a PDF from its library preview. The browser signs a copy of
the bytes shown in the preview with a personal key and embeds a self-signed
certificate. The native path runs without a Documenso service or license.

| Concept | Meaning in Galadril |
| --- | --- |
| Handwritten mark | A personal drawing that can be used as the appearance of a visible signature field. |
| Cryptographic signature | A signature over specific PDF byte ranges. A server seal does not establish that each recipient used their own signing credential. |
| Qualified signature | A request for QES is distinct from a validated QES. Article 3(12) of [eIDAS](https://eur-lex.europa.eu/eli/reg/2014/910/) requires a qualified certificate and a qualified signature creation device in addition to the advanced signature requirements. |

## Ownership and trust boundaries

- **Gateway / IAM:** authenticated account settings call the typed GraphQL
  operations in `services/gateway/src/adapters/inbound/graphql/schema.rs`.
  `application/usecases/pengolo/signatures.rs` rechecks the live user and
  tenant authorization on every operation. The API takes no tenant or owner
  parameter. Traces and signing vaults are accessible to their authenticated
  owner and are unavailable to the model.
- **PostgreSQL:** `202611110001_pengolo_signatures.sql` forces row level
  security on both tenant and owner. The store sets both transaction locally;
  the owner limit is enforced under an advisory transaction lock. The trace
  consists of bounded numeric paths on a normalized grid. Gateway encrypts it
  using the existing `PENGOLO_DOCUMENT_VAULT_KEY` with AES-256-GCM, a random
  nonce, and authenticated data bound to tenant, owner and mark ID. Labels
  and creation timestamps are metadata in PostgreSQL. Deleting a mark removes
  its row; ordinary PostgreSQL backup retention still applies. OTLP events
  identify operations and marks without including the trace or label. Keep the
  vault key available to all Gateway replicas. The separate
  `202611110002_pengolo_signing_vaults.sql` migration stores one encrypted
  private key per tenant and account. An atomic insert prevents a retry or a
  concurrent enrollment from replacing that key.
- **Document Compiler / S3:** the [existing compiler path](document_compiler.md)
  still stores immutable template revisions and publishes through Gateway's
  authorized artifact path with SHA-256 and its existing retention. PDF widgets
  are produced as part of compilation; no postprocessing or new storage access
  is granted to TeX.
- **Registry:** continues to own ontology and pipeline revisions. Personal marks
  and document signature state are not ontology or pipeline entities.
- **Official plugins:** `galadril_plugins.signatures.SignatureProvider` defines
  draft, distribution, status, cancellation and retrieval. The Documenso package
  supplies a transport but declares no tool, pipeline binding or worker. Merely
  discovering it does not start a process or contact an external provider.

## Named fields in LaTeX

With the `recoursadmin` class, add `\GaladrilEnableSignatureFields` to the
preamble (for the packaged project, between `\documentclass` and its inputs).
The request and brief put `galadril:claimant` below the closing date and before
the list of exhibits. The letter puts `galadril:sender` in its closing block.
Use `\GaladrilSignatureSlot{counsel}{136bp}{34bp}` to add another named field.
Names must be unique, start with a lowercase ASCII letter, and contain only
lowercase ASCII letters, digits or hyphens. The class rejects duplicate names.

Tectonic's bundle did not provide `eforms.sty`. The class uses XeTeX/dvipdfmx
specials to emit actual empty AcroForm `/Sig` widgets. This option assumes
the class owns the AcroForm; combining it with another form package is not
supported. Ordinary exports keep their prior layout and have no new widgets.
No field constitutes a consent or a cryptographic signature.

## Documenso compatibility

The adapter uploads a draft only when the bytes match the requested SHA-256,
passes the ordered signer roster and explicit signature rectangles, and keeps
distribution separate from draft creation. The rectangle contract uses
percentages from the top left of an unrotated page. The public Documenso API
[normalizes and can flatten uploaded forms](https://github.com/documenso/documenso/blob/main/packages/trpc/server/envelope-router/create-envelope.ts),
so the adapter takes coordinates separately instead of relying on the widgets
to survive upload. Galadril does not yet extract those coordinates automatically.

The adapter requires the envelope read to confirm the requested CSC tier,
sequential order, request identifier and signer roster before distribution.
The [public V2 response schema reviewed](https://github.com/documenso/documenso/blob/main/packages/lib/types/envelope.ts)
does not expose `signatureLevel`. An instance with that schema will refuse
distribution through this adapter; a compatible vendor API is required. An
unknown or lower tier is never treated as confirmation. Use a dedicated
provider team and operator controlled endpoint, with credentials scoped to the
tenant, if this transport is wired later.

[Documenso CSC](https://docs.documenso.com/docs/self-hosting/configuration/signing-certificate/csc-qes)
describes per-recipient PAdES, direct signer authentication and a final archival
timestamp. Do not bypass its timestamp or trust-service configuration. The adapter
never adds its own terminal seal. It does not use a downloaded PDF, provider
status, or a requested tier to assert cryptographic validity or qualification.

The documented [webhook secret](https://docs.documenso.com/docs/developers/webhooks/verification)
is a shared header value, not a MAC over the payload. The adapter compares it
in constant time and returns only a terminal event hint; it discards recipient
tokens and payload details. A future receiver must authenticate the hint,
resolve the envelope in a trusted local mapping and reconcile with an
authenticated V2 read. Mutating calls do not retry ambiguous responses.
Documenso's `externalId` correlates requests but does not prove idempotency.

## Native signing and evidence

Choose **Sign PDF** in the preview, select a field and a saved mark when a
visible field is available, and confirm consent. The browser fills the
existing visible widget and includes the appearance in the signature's covered
bytes. When there is no available visible field, it adds an invisible signature
field. The downloaded file contains the original bytes followed by the
incremental update. The LibPDF 0.5.3 patch preserves existing widget geometry
and page association during signing.

At first use, the browser generates an RSA signing key and certificate. It
wraps the PKCS#8 private key with AES-256-GCM using a dedicated password, a
random salt, and PBKDF2-SHA-256 with 600,000 iterations. An optional second wrap
uses HKDF-SHA-256 over a compatible authenticator's
[WebAuthn PRF output](https://www.w3.org/TR/webauthn-3/#prf-extension).
Authenticated data binds each wrap to the tenant, account, certificate and
unlock method. Gateway receives the ciphertext and public certificate. The
password, PRF output and plaintext private key stay in the browser. Every
signing operation imports a non-exportable key after unlock. The authenticator
may verify the user with biometrics or a device PIN.

The native path embeds a basic PAdES signature. A PDF verifier can check the
integrity of the signed bytes and their relationship to the embedded key.
The certificate has an opaque identifier. Establishing an advanced signature
under [Article 26 of eIDAS](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX%3A02014R0910-20241018)
also requires evidence that ties the key to an identifiable signer and
demonstrates the signer's control of it. This implementation has no
independently verifiable identity or consent record, trusted timestamp, key
recovery, or rotation. The signed copy is downloaded. Archiving it in the
library is a separate upload.

## Verification

The portable test verifies that password and PRF unlock the same key and that
wrong credentials or a different tenant or account fail. Run the Tectonic
tests from `front/dashboard` with a cached Tectonic bundle and Poppler:

```sh
GALADRIL_SIGNING_E2E=1 node --test tests/pdf-signing.test.ts
```

The Chromium scenario in `library-ui.spec.ts` uses the production LaTeX
class and checks both generated variants with `pdfsig`. It mocks Gateway
transport and uses Chromium's virtual WebAuthn authenticator for PRF. Run it
from `front/dashboard`:

```sh
GALADRIL_SIGNING_E2E=1 GALADRIL_UI_TEST_DEV=1 GALADRIL_UI_TEST_PORT=3157 \
  pnpm exec playwright test -c tests/playwright-pipeline.config.ts \
  library-ui.spec.ts -g 'sign Tectonic PDFs'
```

Both paths use the same certificate after unlocking with different factors.
The PDF tests also verify that a changed byte invalidates the signature and
that an appended countersignature leaves both signatures cryptographically
valid. Rendering with Poppler confirmed that the drawing appeared on the
second page and that the invisible signature left the page rendering intact.

The targeted Bazel PostgreSQL test passed using a non-superuser role and a
one-connection pool. It checks other users, other tenants, pooled connection
reuse, missing scope, deletion of marks and rejection of duplicate vault
enrollment. Gateway tests passed for trace bounds, ciphertext binding and
tampering, and the dashboard GraphQL schema contract. The broader Gateway
suite had one connection reset in the HTTP request body limit test.
The two plugin Bazel targets passed. The PDF test was skipped in Bazel because
the tools are absent from its environment; it passed locally with Tectonic 0.17
and qpdf, checking opt-in, names, physical pages and unsigned values.

Two Chromium scenarios passed against GraphQL mocks: the saved mark lifecycle
and the existing account settings. The new UI was inspected at desktop and
390-pixel widths. Rust Clippy, Ruff and dashboard type checking passed.
Workspace Rust formatting reported differences in unrelated files. The
workspace Bazel run stopped during analysis of the Linux ARM compiler image
because the macOS runner had no matching C++ toolchain. The native tests use
self-signed certificates and require no Documenso envelope.
