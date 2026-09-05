# Galadril authorization contract ✍️

`schema.zed` is authoritative. A SpiceDB allow never bypasses RLS, and an
RLS-visible row never implies permission to use it.

## Trust and identifiers

- JWT verification establishes the user.
- Tenant IDs are normalized once. Resource IDs use `<tenant>/<local-id>`.
- User/role/group resource grants are intersected with `parent->view`, so
  removing tenant membership also removes direct resource access. Service
  execution uses separate explicitly scoped service relations.
- Cedar is a contextual **restriction** after a SpiceDB allow. It never grants
  access by itself and never consumes unsigned context.
- Cross-service context binds actor, tenant, execution identity, action,
  resource, issuer, trace, and optional ZedToken. `authorized=true` is invalid.

## Permission catalog and ownership

| Resource | Permissions | Meaning / boundary | Relationship writer |
|---|---|---|---|
| `tenant` | `view`, `edit`, `delete`, `share`, `manage`, `ingest`, `create_document`, `create_ontology`, `create_pipeline`, `create_conversation` | Tenant administration and creation roots; Gateway before side effects | Gateway IAM |
| `project` | `view`, `edit`, `delete`, `share`, `manage` | Project API operations | Gateway/project owner |
| `table` | `view`, `edit`, `delete`, `share`, `manage` | Dataset query and mutation | Gateway/catalog owner |
| `raw` | `view`, `materialize`, `delete`, `manage` | Ingestion objects and processing | Intake asserts trusted context; Vision materializes |
| `document` | `view`, `edit`, `delete`, `share`, `manage` | Document operations | Document owner |
| `ontology` | `view`, `edit`, `delete`, `publish`, `manage` | Ontology inspection, publication, and retirement | Gateway/ontology owner |
| `pipeline` | `view`, `execute`, `edit`, `delete`, `publish`, `manage` | Pipeline inspection, dispatch, versioning, publication, and retirement | Gateway/pipeline owner |
| `conversation` | `view`, `edit`, `delete`, `manage` | Durable Scribe conversation and message operations | Gateway/conversation owner |
| `entity_state` | `view`, `edit`, `delete`, `manage` | Entity-state read and mutation | Vision |
| `event` | `view`, `manage` | Event read and repair | Vision |

Only Gateway writes tenant/role membership. Only Vision writes `raw`,
`entity_state`, and `event` resource relationships, from an Intake-established
trusted envelope. Writers reject relationships outside their ownership allowlist.

| Relationship category | Sole writer | Preconditions |
|---|---|---|
| `tenant#member`, `tenant#administrator`, `tenant#role` | Gateway IAM | authenticated tenant administrator |
| `role#parent`, `role#member` | Gateway IAM | role and user resolved in the same tenant |
| `raw#parent`, `raw#owner` | Gateway upload finalizer | authenticated tenant member completed an authorized upload |
| `raw#reader`, `raw#processor` | Vision authz materializer | Intake delegation matches object tenant/resource |
| `entity_state#parent`, `entity_state#source` | Vision authz materializer | tenant data and outbox row committed together |
| `event#parent`, `event#source` | Vision authz materializer | tenant data and outbox row committed together |
| `ontology#parent`, `ontology#owner` | Gateway ontology publication | validated tenant materialization exists |
| `pipeline#parent`, `pipeline#owner` | Gateway pipeline authoring | immutable root revision committed |
| `conversation#parent`, `conversation#owner` | Gateway conversation service | durable conversation committed |
| project/table/document relations | owning domain service | not yet implemented in this repository |

## Gateway enforcement catalog

| Operation | Resource | Permission | Downstream context |
|---|---|---|---|
| GraphQL search/event results | `entity_state`, `event` | `view` | result IDs remain tenant-qualified |
| GraphQL entity exploration | `entity_state` | `view` | every returned graph node is rechecked |
| `requestStagingUpload`, `completeUpload` | tenant | `ingest` | issuer + actor + tenant + raw target + delegation ID |
| tenant user/role administration | tenant | `manage` | Gateway-owned relationship writes |
| `setCedarPolicy` | tenant | `manage` | validated policy, audit event, cache invalidation |
| AI subscription | conversation | `edit` | request-scoped Scribe tools preserve identity, RLS, SpiceDB, Cedar, trace, and audit |

Tenant-specific Cedar policy is a deny-only contextual layer after a structural
allow. Administrators manage it through Gateway's `setCedarPolicy` mutation.
See `examples/contextual-constraints.cedar`; its facts are signed IdP claims,
not headers or JSON fields.

## Advices

- Ordinary discovery may use minimize-latency consistency.
- Grant-then-use and create-then-use use at-least-as-fresh with `written_at`.
- Revocation, destructive operations, administration, and delegation issuance
  use fully-consistent or at-least-as-fresh with the revocation token.
- SpiceDB errors and timeouts fail closed.
