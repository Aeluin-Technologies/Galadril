# Document signatures

Galadril can sign a PDF from its library preview. The browser signs a copy of
the bytes shown in the preview with a personal key and embeds a self-signed
certificate.

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

## Native signing and evidence

Choose **Sign PDF** in the document actions menu. Keep or move an existing
field, or drag a rectangle on a PDF page to create a field. PDFs without a
predefined field can also be signed without a visible mark. A visible mark can
include the account display name and local signing date in English or French.
Confirm consent and unlock the key, then download the signed PDF in the final
step. The original bytes precede the incremental update, and the signature
covers the appearance and caption.

The MCP tool `request_document_signature` requires `documents:read`. Gateway
checks live file authorization and returns a link on the configured Galadril
origin. The browser checks access again under the signed-in user's session
when opening the PDF and before signing. The tool creates no signature and
receives no signing credentials.

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
An enrolled PRF signing passkey takes precedence. Password unlock is shown
when the browser reports no compatible platform authenticator or reports that
the credential cannot be found. WebAuthn's ambiguous cancellation error
does not unlock password fallback. Login passkeys and PRF signing enrollment
are separate.

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
The PDF test was skipped in Bazel because
the tools are absent from its environment; it passed locally with Tectonic 0.17
and qpdf, checking opt-in, names, physical pages and unsigned values.

Two Chromium scenarios passed against GraphQL mocks: the saved mark lifecycle
and the existing account settings. The new UI was inspected at desktop and
390-pixel widths. Rust Clippy, Ruff and dashboard type checking passed.
Workspace Rust formatting reported differences in unrelated files. The
workspace Bazel run stopped during analysis of the Linux ARM compiler image
because the macOS runner had no matching C++ toolchain. The native tests use
self-signed certificates.
