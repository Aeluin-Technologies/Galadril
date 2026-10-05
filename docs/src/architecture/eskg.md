# ESKG graph contract (v1)

The instance graph records evidence, identities, and temporal structure.
Registry owns ontology definitions and their immutable revisions. An ontology
resource is never an instance-graph vertex label or an authorization namespace.

## Vertices

Every vertex has one structural kind, a non-empty opaque `id`, and a `tenant_id`.
Identity is `(tenant_id, id)`; identifiers do not encode a vertex's kind.
The AGE label and the API `kind` are the same structural kind:

| Kind | Meaning | Optional ontology resource kind |
| --- | --- | --- |
| `Entity` | Authoritative identity of an observed physical entity | `object_type` |
| `Event` | Immutable observation or occurrence | `event_type` |
| `State` | A property observation of an entity at an event time | `property` |
| `LatentIdentity` | Unresolved identity hypothesis | `object_type` |
| `Inference` | Evidence-backed interpretation | none |
| `Decision` | Identity-resolution decision | none |
| `CausalVariable` | A measured variable used by causal inference | `property` |
| `Metric` | Operational metric, outside the base ESKG | none |

`Person`, `Observation`, and `Transaction` are classifications, not structural
kinds. Extractor strings may be retained as `observed_type` or `event_type`;
they confer no ontology membership. An unclassified instance has no ontology
reference. `Person` would describe an object type, not a property.

An optional `ontology_ref` is an indivisible record containing `tenant_id`,
`ontology_id`, `revision_id`, `resource_id`, and `resource_kind`. The revision
is the opaque immutable Registry revision, never a display name, ontology
version string, pipeline revision, or floating production publication.
The producer resolves the resource from its active block-local Registry slice,
checks its resource kind and tenant, and copies the revision from that slice.
Missing resources, partial references, and incompatible kinds fail closed.
Reinterpreting evidence under a new revision must not rewrite its old reference.

The Vision sink accepts `ontology_resource_id` and
`event_ontology_resource_id` parameters; resolved items may additionally carry
their own `ontology_resource_id`. These identifiers must be included in the
sink block's Registry binding. The sink derives all other reference fields from
the active slice; callers cannot select a different revision through a payload.

## Relations

Relation labels are a closed structural vocabulary. Direction is significant.
Both endpoints and the edge belong to the same tenant. A mutation succeeds only
if both endpoints exist with a permitted ordered pair of structural kinds.
Unknown relation names and invalid endpoint pairs are rejected before mutation.

| AGE relation | Permitted source → target |
| --- | --- |
| `TRIGGERS` | State → Event |
| `LEADS_TO` | Event → State |
| `EVOLUTION` | State → State |
| `CONTAIN` | Event → Event |
| `OCCUR`, `INFLUENCE` | Event → Entity |
| `HAS_INFERENCE` | Event → Inference |
| `CONSIDERED_CANDIDATE` | Inference → Entity or LatentIdentity |
| `HAS_DECISION` | Inference → Decision |
| `SELECTED_TARGET` | Decision → Entity or LatentIdentity |
| `PROMOTED_AS` | LatentIdentity → Entity |
| `MERGED_WITH` | LatentIdentity → LatentIdentity |
| `SUPERSEDES`, `REVOKES` | Same-kind Event, State, Inference, Decision, or LatentIdentity |
| `STATE_OF` | State → Entity |
| `APPEARS_IN`, `PARTICIPATED_IN`, `DERIVED_FROM`, `OBSERVED` | Entity → Event |
| `MENTIONS`, `DESCRIBES` | Event → Entity |
| `CAUSES` | CausalVariable → CausalVariable |
| `METRIC_INFLUENCE` | Metric → Metric |

The last five rows are Galadril extensions. In particular, metric influence is
distinct from the base ESKG's Event → Entity influence. A business link type
does not become an AGE relation label; adding business-link instances requires
a separate versioned-reference contract and is not supported by v1.

## Reads and authorization

Traversal accepts only the relation vocabulary above, checks endpoint kinds
and edge/vertex tenants, and preserves stored direction. Event discovery selects
`Event` structurally, including incoming `OCCUR`/`INFLUENCE` links; a timestamp
or an identifier prefix does not establish that a vertex is an event.
Gateway exposes structural `kind` and a separate `ontologyRef`. Its existing
`label` field is a compatibility alias for the structural kind.

Gateway maps Entity to `entity_state` and Event to `event`. State uses its
explicit `entity_id` and source `event_id` and requires permission on both.
Other kinds currently require tenant management permission. Restricted graph
projections retain structural kinds but redact ontology references and arbitrary
properties. Only edges incident to an authorized Event are currently exposed
to restricted callers; causal and hypothesis provenance need their own policy.

## Current materialization and rollout

Vision materializes Entity and Event vertices, operational Metric vertices, and
CausalVariable vertices. State observations remain in TimescaleDB; transition
and identity-hypothesis graph materialization are not implemented by this change.
Keeping those roles in the vocabulary does not assert that their facts exist.

The architecture is not deployed (see [Registry initialization](ontology.md)).
Prototype graphs with business labels or arbitrary edges are incompatible with
v1 and must be recreated explicitly from source evidence. Readers do not guess
structural kinds from old labels, and no automatic destructive reset is run.
A deployment with durable legacy evidence would require a separately reviewed
replay/migration preserving AGE identities, relation direction, and provenance.
