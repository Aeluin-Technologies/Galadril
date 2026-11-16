# GOL ontology authority

Registry owns the GOL source lifecycle and invokes `galadril-ontology` for parsing,
semantic analysis, security compilation and archive production. Sources are
versioned `.gol` files in lakeFS. RustFS holds the active `.golc` generation and
its atomic publication manifest. JSON Schema is an authorized projection of that
compiled model, never a second ontology definition.

See [Registry GOL lifecycle](gol-registry.md) for source revisions, imported
module ownership, publication, deletion and immutable pipeline bindings. See
[the GOL dashboard](gol-dashboard.md) for authoring and authorized exploration.
The previous JSON ontology graph, inheritance resolver, semantic merge, slice
RPC and branch/trash authoring model have been removed.

Gateway authenticates the caller and applies coarse ontology permissions plus
the shared SpiceDB, Cedar and contextual IFC gates. Source text and compiler
diagnostics require explicit authoring admission; read-only exploration obtains
only authorized descriptions and projections from the compiled archive. Vision
receives generation-bound descriptors through Gateway's admitted runtime API.
It does not parse GOL, receive source-storage credentials or recreate a semantic
model in Python. Instances and observations remain ESKG responsibilities.

Pipeline definitions, pipeline collaboration and tenant repository management
remain Registry contracts. Pipeline-to-GOL bindings preserve tenant, source
revision, generation and selected symbol identities as an indivisible pin.
Deleting a current pipeline removes its current bindings in the same source
commit; immutable historical pipeline revisions keep their original evidence.

Internal Registry RPCs are deployment trust boundaries. Public clients use
Gateway; workload routes are separately constrained by their service identity.
Neither source paths, lakeFS repository names, S3 coordinates nor raw publication
candidate catalogs belong in a browser response. Tenant discovery remains a
bounded internal capability of Intake and Vision, separate from user-facing
ontology listing.
