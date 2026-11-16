# Authorized JSON contracts

Enable `galadril-ontology/export` to project validated GOL metadata. It is
optional and does not add an inference engine. `ProjectExporter` compiles the
security environment against the same validated `semantic::Project` it reads.
`ArchiveReader::export_schema` uses authenticated definition blocks already
present in GOLC v1. `RemoteArchive::export_schema` adds the existing RustFS
snapshot deadline and tenant fence. Neither archive path requires `.gol`
sources, the parser, nor the semantic compiler. The binary format is unchanged.

## API and explicit selections

`ExportRequest` borrows a `Projection`, `ExportOptions`, and an
`ExternalMappings` registry. A root `Selection` contains an actual instance
access intent and a sorted whitelist of candidate fields. Empty means no
fields, never all fields. Named value types and `Ref<T>` targets need separate
`Projection::definitions` selections with their own verified instance context.
Unknown and denied fields are omitted identically. If a nonempty root selection
has no visible field, export returns `Unavailable`.

```rust,ignore
let options = ExportOptions {
    profile: SchemaProfile::OpenAiStrict,
    ..ExportOptions::default()
};
let request = ExportRequest {
    projection: &projection,
    part: SchemaPart::Value,
    options: &options,
    mappings: &mappings,
};
let contract = archive.export_schema(&request, &runtime, &spicedb).await?;
contract.validate_json(&candidate)?;
```

The same engine serves API contracts and MCP action schemas. Select an action
and use `SchemaPart::ActionInput` for `inputSchema`, then
`SchemaPart::ActionOutput` for `outputSchema`. The result has an object envelope
with a required `result` property, including scalar or unit results. Required
action parameters cannot be silently excluded. No action is executed and no
execute permission is granted.

## Security boundary

`Operation::Describe` is a distinct metadata permission, selected explicitly by
the trusted deployment in `SecurityEnvironment::routes`. Existing read routes
do not automatically grant it. Its descriptor inherits the root and field's
SpiceDB bindings, Cedar conditions, IFC floor and dependency obligations.
`DescribeTransfer` additionally inherits Describe and requires its own transfer
bindings, actual sink and purpose. Read permission alone never permits export
to an external LLM. No new authorization engine or decision cache is introduced.

The root, each candidate property, and each used named definition are checked
through the phase 4 gate before disclosure. A denied type suppresses its
property. Its name and `$defs` entry are not emitted. `Ref<T>` emits a closed
`{type, id}` object containing an approved versioned logical identity and an
opaque nonempty ID. It never traverses the target graph. External relations,
derivations and views keep the existing conservative dependency obligations.
Relation and view definitions themselves are not JSON records in this profile.
Descriptions, source locations, annotations and policy bodies are deliberately
not copied into public contracts. Error categories contain no hidden names.

Schemas are request-scoped results. Re-export always repeats SpiceDB, Cedar
and IFC decisions, even when all binary fragments are cached. No schema cache
exists, so neither principal nor context can reuse another decision. The host
must re-export before subsequent disclosure and must not treat a previously
returned schema as a permission lease. Context, identity, clock, location,
resource state, ZedToken consistency and export destination come from the
existing trusted runtime. `tracing` emits bounded `gol.export.unavailable`
events without field names or candidate contents into the host OTLP pipeline.

## Representations and precision

| GOL value | JSON representation |
|---|---|
| Entity / struct | Closed object, independently selected fields |
| Simple enum | String `enum`, sorted variants |
| `bool`, `String`, unit | Boolean, string, null |
| `char` | One Unicode scalar, standard profile |
| `i8` through `i32`, `u8` through `u32` | Integer with exact bounds |
| All signed/unsigned Rust integers through 128 bits | Explicit `DecimalStrings`, exact canonical decimal range regex |
| `i64`, `u64`, `i128`, `u128` as numbers | Rejected because the full domain exceeds exact binary64 integers |
| `f32`, `f64` | Finite bounded JSON number only with `allow_float_rounding` |
| `Option<T>` | Nullable `anyOf`, optional property in standard profile |
| Bounded `List<T>` | Array with `items` and canonical `maxItems` |
| Alias / nominal scalar attribute | Authorized `$defs` / `$ref`; optional aliases retain optionality |
| Registered external scalar | Explicit host-approved, versioned JSON mapping |
| `Ref<T>` | Typed opaque ID, without recursive entity expansion |

`DecimalStrings` applies consistently to every integer in the projection. It
rejects leading zeros, negative zero, exponent notation and out-of-range
magnitudes. The default never guesses an encoding or narrows a Rust domain.

An `ExternalMapping` is registered against an exact `ExternalTypeId`. Its
`VersionedName` identifies the adapter contract; duplicate registrations fail.
The supported mapping vocabulary is bounded scalar JSON Schema (`type`,
`enum`, numeric bounds, string length/pattern, and known date/time/UUID formats).
Remote `$ref`, arbitrary schema keywords and unknown formats fail registration.
Integer mappings require safe numeric bounds. The host attests that its actual
serializer preserves the claimed semantic domain. The constructor validates
the schema and its format vocabulary, not arbitrary Rust adapter code. A
`Rounded` mapping additionally requires explicit precision acceptance.
`chrono` and `jiff` are not discovered or imported. The fixture registers an
RFC 3339 timestamp with fractional seconds; a date, time, timezone or
nanosecond instant needs its own exact mapping and serializer contract.

## Concrete profiles

| Profile | Required and nullable fields | Explicit restrictions |
|---|---|---|
| `Standard` | Non-Option fields required; Option may be absent or null | JSON Schema Draft 2020-12 |
| `OpenAiStrict` | Every selected property required; Option accepts null | Object root, closed objects, conservative eight-level expansion cap; string length keywords rejected |
| `Outlines` | Standard optionality and nullability | Tested against outlines-core **0.2.14**; numeric bounds rejected, so bounded integers require explicit decimal strings; competing string pattern/format/length and enum constraints rejected |

Profiles preserve `$defs`, local `$ref`, exact ranges and list limits. A
mandatory constraint is never removed to obtain compatibility. The OpenAI
profile is a conservative subset of the [provider's documented structured
output schema](https://developers.openai.com/api/docs/guides/structured-outputs).
It does not assert acceptance by other providers, fine-tuned models, or a live
API. Outlines compatibility uses its official
[`json_schema::regex_from_value`](https://docs.rs/outlines-core/0.2.14/outlines_core/json_schema/fn.regex_from_value.html)
entry point, without loading a model or generating tokens. Its optional
`outlines-compat` feature is needed only for compatibility tests; the upstream
crate still has tokenizer dependencies even with default features disabled.

Defaults limit expansion to 12, counted properties/enum values to 256 and
serialized schema/candidate size to 128 KiB. Hard ceilings are 32, 1024 and
1 MiB. Definitions are normalized by logical identity; keys use unambiguous
`module::Name@version` identities. Object keys, selections and required arrays
are sorted. Results do not depend on declaration order. JSON validation uses
`jsonschema` 0.61.2 with format assertions enabled and an explicit deny-all
retriever; it cannot fetch a schema URL or open a referenced file.

## Structural validation and ingestion

`SchemaDocument::validate_json` validates only the visible JSON structure.
An object may pass it and still violate GOL invariants, referential integrity,
action preconditions, or write permissions. It is not an ingestion grant.

`validate_proposal` rebuilds the schema with current authorization, checks the
entity and every supplied field through the ordinary phase 4 **Write** gate,
then invokes a mandatory trusted `GolValidator`. This host interface must
validate the candidate together with current state, omitted protected fields,
references and all applicable GOL obligations. `Rejected` includes unavailable
checks. There is no permissive default implementation. The library never
persists the object. A host must keep final validation and mutation consistent
in its own transaction; this API does not eliminate that write-time race.
The E2E fixture's validator checks its declared nonempty-title obligation. It
is not a general-purpose evaluator of arbitrary GOL expressions.

Whole nested struct assignments are denied at the submission gate, including
through aliases, Option and List. Phase 4 cannot yet bind writes to those
individual members, so a parent field grant cannot authorize their replacement.
Their JSON structure remains exportable and locally validatable. Scalar,
enum, external scalar and opaque reference assignments can proceed to the
host's mandatory validator after their write checks.

## Unsupported conversions and Avro

Owned-attribute multiplicities require an explicit value/array mapping and are
currently rejected. Unbounded lists, recursive value schemas, relation records,
views, general invariant-to-JSON translation, reference resolution, automatic
adapters and arbitrary provider dialects are not inferred. Missing permissions,
contracts or adapter mappings fail closed. JSON Schema does not replace GOL
business validation.

Avro is deferred because the same mappings are not lossless automatically.
A future adapter needs these explicit contracts before exposing events.

| GOL / JSON concept | Required Avro decision |
|---|---|
| Struct / enum | Stable Avro full names, enum symbol names and evolution rules |
| Option | Ordered null union, explicit defaults and absence semantics |
| `i64`, `u64`, 128-bit integer | `long` only when safe; otherwise fixed/bytes decimal logical type with declared precision/scale |
| Temporal external types | Separate date, time and timestamp logical units; timezone and precision contracts |
| Typed `Ref<T>` | A named ID record with preserved ontology/type/version identity |
| Partial authorized record | Security-context-specific contract identity, avoiding a shared schema that reveals hidden fields |
| Action / entity | Explicit event model and lifecycle; neither automatically denotes an event |

No Kafka or Schema Registry component is added.

## Verification

`tests/export.rs` covers IR/archive equality, deterministic output, protected
and unknown metadata, aliases, nested records, enums, nullable lists/references,
external timestamps, precise integer domains, mapping rejection, profile
limits, action input/output and actual Outlines conversion. Integer tests
exhaust the i8 and u8 boundaries in both encodings. Existing archive tests keep
checking deterministic GOLC bytes and selective access.

`export_matrix::docker_export_from_rustfs_preserves_all_security_layers` in the
existing `archive_spicedb` suite runs the source-to-RustFS-to-JSON chain with
pinned RustFS and SpiceDB Testcontainers, official Cedar and phase 4 IFC. It
checks root/field denials, cross-ontology protected references, hidden derived
metadata, MFA, location, absent context, Cedar error, export prohibition,
ZedToken grant/revocation at warm cache, local/remote equality, external and
nested values, strict nullability, write denial, mandatory business rejection,
and unavailable SpiceDB. Containers are isolated from production data.

```sh
bazel test //platform/ontology/rust:export_test
bazel test //platform/ontology/rust:export_outlines_test
bazel test //platform/ontology/rust:remote_security_test
```

The Docker target is explicitly `manual`, `local`, and `requires-docker` and
runs the ignored Docker tests. Outlines is a manual compatibility target.
Runtime-only Cargo builds can use `--no-default-features --features export`
or `--no-default-features --features remote,export`. Keep the workspace Bazel
gate separate from targeted results when platform toolchains prevent analysis.

On 2026-11-15 the 11 scoped Bazel targets passed, including all four real
Docker scenarios and the Outlines compatibility target. The measured export
read 21,932 bytes in 13 archive GETs from a 90,632-byte artifact (24.2%).
After grant/revocation, the warm projection needed zero additional archive
GETs while authorization was rechecked. These counters exclude manifest
traffic, HTTP headers, SDK retries and security-engine requests. They describe
this fixture, not a general latency or throughput guarantee.

The workspace `bazel test //...` gate stopped during document-compiler
toolchain analysis because no matching C++ toolchain was available. Scoped
Rust formatting, strict all-target/all-feature Clippy, Cargo regression tests
and parser-free export builds passed. Workspace nightly formatting reports
pre-existing differences in the Gateway IAM/directory files.
