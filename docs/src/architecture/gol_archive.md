# GOLC local archives

The `galadril-ontology` crate feature `archive` enables a permission-checked
local reader. Add `parser` to produce artifacts with
`archive::compile_archive(&project, &environment)`. The writer compiles
security for the same checked project that it serializes. It rejects invalid
or pending security compilation before writing any artifact.

## Container format v1

The 64-byte little-endian header contains the `GOLC` magic, format revision
(`u32`), archive length (`u64`), root offset (`u64`), root length (`u32`),
reserved zero word, and SHA-256 of the root. The publisher supplies the SHA-256
of this header independently of the bytes to open. A caller that computes an
`ArchivePin` solely from an untrusted file has established no provenance.

The root is the last block. Every independent FlatBuffer has the `GOLB` file
identifier and the schema in `platform/ontology/rust/wire/block.fbs`. Directory
pages use up to 32 sorted entries with inclusive upper keys and absolute
child ranges. Each entry carries the child's digest. Leaves match exact keys.
A child must end at or before its parent offset, so references cannot form
cycles. Readers accept at most eight directory levels and 8 MiB per block.
All fetched ranges are validated against their trusted digest and verified by
FlatBuffers before access to a record. Verification is demand driven. Damage to
an unread range is detected when it is requested.

Typed symbol projection blocks store a declaration kind and, when applicable,
a built-in scalar type. Private definition blocks retain the complete canonical
declarations, expression bodies, effects, import pins, and annotated
requirements. The v1 payloads for those structured records use canonical GOL
JSON inside the typed FlatBuffer blocks. Policy payloads use Cedar's JSON EST
and the archive preserves the exact schema fragments needed for complete
linking. Security block keys index the resolved obligations per target and
operation. Local and external semantic dependencies remain in private records.
The forward and inverse disclosure indexes use exact endpoint keys.

The checked-in Rust bindings are regenerated using official `flatc` 25.12.19:

```sh
flatc --rust -o platform/ontology/rust/wire/src platform/ontology/rust/wire/block.fbs
```

## Local reader boundary

A trusted host opens `ArchiveReader::open(LocalFile::open(path)?, pin)`, using
the immutable pin selected for its deployment. `RangeReader` also allows an
instrumented source. Opening reads the manifest, directory path to the
authority block, and linked Cedar state once. A point query loads the exact
route and its required security descriptors, calls the phase-four gate, and
then reads the targeted symbol projection. Index pages have a bounded cache.
Decisions and symbol payloads are checked on every consultation.

- `read` returns one requested symbol or member. Its result contains no
  dense symbol IDs, neighboring records, security metadata, or full
  declaration bytes.
- `project` releases an entire requested projection only after every
  requested member is allowed.
- `discover` only considers the supplied candidates and emits the visible
  ones. The caller receives no count or list from the underlying directory.
- `links` checks the anchor and each candidate. A link from a field is indexed
  by that field, including on reverse lookup. Granting read access on its
  containing symbol does not disclose the member's link. Dependencies exposed
  through the reader are the explicit member links, view inputs, and derived
  inputs for which the gate has the pertinent controls.

Missing, denied, or unreadable point requests produce the same empty result.
No query returns a raw archive offset, a block, or an error containing private
record metadata. The file, publication pin, permission adapter and runtime
identity provider are host capabilities. Keep the file and range source
inside the trusted deployment boundary. Raw disk access is outside the
disclosure API. The format provides integrity and authenticated sparse reads
when the publication pin is trusted. It provides no encryption or
timing-oblivious access.

This interface releases ontology metadata. Instance values and the binding
of actual dependency instances to a request remain the responsibility of the
trusted runtime described in [GOL Security Contracts](gol_security.md).
A permit covers this request and its supplied evidence. Independent requests
may observe different relationship revisions. No production Gateway route or
service binding is installed by this phase. The same format is available
through [Remote GOLC Publication and Reads](gol_remote.md).

## Tests

The local suite uses an instrumented file backed by a temporary directory.
It compares sparse reads with the in-memory gate, checks byte-for-byte
determinism, and records byte counts, offsets and range counts before and
after the index cache warms. Run it with:

```sh
cargo test --locked -p galadril-ontology --test archive -- --nocapture
```

The Docker suite uses Testcontainers with SpiceDB `v1.56.2` and
`serve-testing`, isolated in-memory state, and a loopback-only HTTP port.
It writes a schema and relationships through the real SpiceDB API. Cedar
runs through the official Rust SDK; the IFC checks come from the compiled
contract. Clock, MFA, location, resources, and session are fixture evidence
issued at the trusted runtime boundary. Run the Docker suite explicitly:

```sh
cargo test --locked -p galadril-ontology --test archive_spicedb -- --ignored
```
