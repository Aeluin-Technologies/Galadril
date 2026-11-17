# Registry GOL lifecycle

Registry owns GOL source packages, compilation and activation. A package is
identified by a qualified primary module name. It may contain companion
modules, but must declare its primary identity. The authored files are UTF-8
`.gol` objects in lakeFS; the catalog stores paths and authoring metadata, not
another ontology representation. Pipeline definitions and collaboration remain
independent of this migration.

Ordinary catalog, pipeline and binding reads open only metadata. Raw sources
are fetched explicitly for one admitted package at an immutable source
revision. Pipeline updates retain source objects without restaging them. A
syntax-only module/import inventory supports dependency deletion guards; it
never substitutes for the GOL compiler or turns an unfinished draft into a
validated model.

`SaveGolOntology` accepts unfinished source text. It enforces relative paths,
unique filenames, at most 32 files, 1 MiB per source and 4 MiB per assembled
compilation. Updates require the immutable tenant revision returned by a prior
read. Source revisions are lakeFS tenant commits: unrelated pipeline edits can
advance the current revision without changing the GOL text.

Each write stages on a private lakeFS branch created from the expected immutable
revision. Its committed state is merged into the tenant branch without a forced
conflict strategy. A shared write witness changes on every Registry commit,
including disjoint pipeline/source edits, so concurrent writes conflict rather
than interleaving their staging areas. Source paths include the fingerprint of
the complete normalized source package; reading or staging source bytes verifies
that fingerprint. This keeps the catalog and its `.gol` objects consistent.
These operations use the [pinned lakeFS 1.88.0 API](https://github.com/treeverse/lakeFS/blob/v1.88.0/api/swagger.yml).

Private branches are deleted after success or failure with a bounded cleanup
request. Cleanup failures are logged separately from the result of an already
visible commit. An interrupted process can leave an unmerged private branch;
it does not activate source changes and requires operational cleanup. Tenant
writes must all pass through Registry for the shared witness to cover them.

`ValidateGolOntology` invokes the shared GOL parser, semantic analysis and
security compiler. It returns stable error codes, filenames and half-open UTF-8
byte spans. No imported text is opened as a local path or fetched from a
caller-selected URL. Imported modules must have an active publication in the
same tenant, under their primary module identity. Their immutable source
revisions and generations are captured explicitly. GOL performs version and
type checks over the assembled sources.

The approved module-owner map resolves companion modules to their source
package. It also prevents one package from redeclaring a module owned by
another package. Imports are admitted incrementally: Registry parses the
already authorized source files and requests authorization for the next source
package before opening it. Gateway applies the conservative source-authoring
gate to every requested package. Only a complete, immutable dependency plan can
reach semantic compilation. A changed generation, source revision or authority
profile invalidates that plan before imported diagnostics are produced. This
currently requires management access to imported sources; a separate public
compiled-import contract is not implemented.

A cyclic publication dependency that reintroduces an older generation of the
root package is refused with `gol::publication_cycle`. This is an operational
publication conflict, not a GOL syntax or type error. Cyclic business relations
and imports between approved companion modules in one package remain valid.
Atomic publication across multiple packages is not implemented.

`PublishGolOntology` compiles that exact source revision into `.golc`, stages
and verifies the archive, then activates it with a manifest compare-and-swap.
The source revision, dependency generations, module versions and private symbol
candidates are committed in that same manifest update. The RustFS manifest is
the sole active-generation authority; lakeFS never stores a competing active
pointer. A failed compiler, unresolved external approval, upload or CAS cannot
activate a partial generation.

Before activation, Registry rechecks the authenticated publisher's current
SpiceDB permission and provisions only that actor's reserved `__schema`
relationships. These tuples grant metadata access subject to tenant, ontology,
Cedar and IFC checks. They do not grant instance access or mailbox ownership.
Failure leaves the staged generation inactive.

Deletion first removes catalog membership at an optimistic lakeFS commit, then
deactivates the RustFS generation. Publishers recheck source membership between
staging and activation. Deactivation fences earlier staged writers, including
an initial publication whose expected generation is absent. Existing reader
handles retain their bounded lease; a new reader cannot reopen a retired
generation and prolong its retention. Pipeline bindings block source deletion.

## Approved host configuration

Publication requires the backend-only `GolAuthorityConfig`. Its fields pin the
relationship schema, object-ID bridge, Cedar schema and policies, operation
routes, IFC baseline and module floors, sinks, projected Cedar attributes and
external adapters. Permission names come from the approved relationship schema
snapshot, including separate permissions such as `mailbox_owner`; they are not
inferred as grants from serialization traits or source declarations.
The shipped sources use local permission names `read_access` and `write_access`
because `read` and `write` are reserved effect keywords. Their external SpiceDB
permissions and Cedar action identifiers remain `read` and `write`.

Source authors cannot submit this configuration. Missing approvals leave
publication unavailable. There is no generated permit policy, implicit
clearance grant or fallback from a field permission to generic `read`.
Owner-specific Cedar resource types are derived from the complete logical
symbol identity, while approved action identifiers can be shared by multiple
resource types.

The service loads the approved JSON file from `GOL_AUTHORITY_CONFIG_PATH` and
uses `GOL_ARTIFACT_BUCKET` for unversioned compiled artifacts. That bucket must
be distinct from the lakeFS source bucket. Credentials and storage coordinates
stay in trusted service configuration.

`infrastructure/gol` contains the explicit deployment profile and fingerprints
of its relationship schema, bridge, Cedar schema and policies. Its authoring
floor is distinct from the caller's clearance. External LLM transfer is limited
to Internal data for extraction or classification; an internal read never grants that transfer.
The E2E profile has a different fingerprint and deliberately does not require
location, because the real test OIDC issuer does not attest it. Production
requires the trusted location context and denies access when it is absent.

## Runtime boundary

Pipeline bindings record an indivisible ontology/source/generation/symbol
selection at an immutable pipeline revision. `GetGolBinding` returns those
backend pins; it does not return an ontology graph or confer authorization.
Gateway admits the workload and uses the common sparse reader and GOL security
gate before describing symbols, properties, references or JSON Schemas. The
private manifest candidate list is never a user-visible catalog.

Every GOL RPC additionally requires the Gateway-only credential loaded from
`REGISTRY_GATEWAY_KEY_PATH` or the client's explicit `gateway_key_file`.
Missing credentials leave those RPCs unavailable, including on the local
loopback proxy. Pipeline discovery remains a separate workload capability.
The deployment mounts the 32-byte credential only into Gateway and Registry;
the E2E harness generates an isolated key rather than checking one into source
control. This authenticates the backend channel, while Gateway remains
responsible for the user and context supplied across it.

For Docker, provision a 32-byte file at
`GALADRIL_REGISTRY_GATEWAY_KEY_PATH` before starting Registry and Gateway. Bind
mounts refuse to create an absent credential path. Kubernetes expects the
`registry-gateway-key` secret with a `gateway-key` entry, mounted only in those
two deployments. Runtime effect receipts use a separate `gol-effect-key` secret;
an absent effect key disables effects without granting a read bypass. Do not
reuse either key as a SpiceDB credential.

The services accept only their documented container mount paths for GOL
configuration and credentials. Change the host-side mount source to select a
different file. The mount remains read-only inside the container, including
for Kubernetes projected volumes that rotate files through symlinks.

Gateway and Vision additionally share a separate 32-byte workload key via
`GOL_WORKER_KEY_PATH`. Docker uses `GALADRIL_GOL_WORKER_KEY_PATH`; Kubernetes
expects `gol-worker-key` with a `worker-key` entry. Only Gateway and Vision mount
it. The internal runtime/effect HTTP routes require this credential in addition
to their workload and computation admission checks. Registry, browser clients
and plugins never receive it. Production service identity controls remain in
force; a local loopback address is not treated as proof of workload identity.

The logical authenticated tenant is distinct from the safe storage namespace.
Hashing a tenant into an object-key segment never changes the tenant checked
by the runtime gate.

## Explicit limits

Source and artifact activation span different stores. A source-delete commit
can succeed before artifact deactivation fails; Registry returns an error and
the operation must be retried, rather than claiming a completed deletion.
Publication metadata is bounded by the 64 KiB operational manifest. Oversized
catalogs fail explicitly instead of silently dropping candidate names.
Generation leases are bounded; processing must finish within its admitted
snapshot lifetime. There is no unbounded artifact history, compatibility JSON
ontology model, parser in Vision, or executable action implementation here.
