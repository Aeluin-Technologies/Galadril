# Remote GOLC publication and reads

Enable the `remote` Cargo feature for RustFS/S3. The adapter uses the same
AWS S3 SDK already pinned in the Galadril workspace. It needs a trusted S3
client and a private, dedicated, unversioned bucket. The backend owns the
tenant and project namespace under `gol/v1/<tenant>/<project>/`. End users
receive checked symbols and links. Storage credentials, object paths, the
manifest and the range source stay at the backend boundary.

The existing GOLC v1 bytes are unchanged. The compiler still checks the GOL
project, links security, and emits independently verified FlatBuffer blocks.
Source revision history remains the responsibility of the lakeFS source
workflow.

## Activation and cleanup

1. `RemoteStore::connect` checks bucket versioning. Its isolated probe tests
   create-only writes, rejection of stale ETags, rejection of one of two
   concurrent updates using the same ETag, and the result read back from the
   store. Unsupported configurations fail closed.
2. `stage` compare-and-swaps an entry into the operational manifest, uploads
   the unique artifact key with a create-only request, and streams it back
   under its ETag to verify its complete SHA-256 and length. The prior active
   artifact remains usable during staging.
3. `Staged::activate` uses the current manifest ETag to activate the staged
   artifact and record temporary retirement for the previous ID in the same
   write. The expected previous active generation is compared on each retry.
   A failed or abandoned stage is reclaimable by the same cleanup protocol.
4. A backend maintenance loop holds the result of `observe_cleanup` and
   invokes `sweep` once the relevant interval has elapsed. It may retry
   earlier and receives `Retained` while candidates are still protected.
   The reaper first fences the candidate through a manifest CAS, then
   deletes its immutable object, then removes the retirement entry. A
   restarted reaper can resume fenced deletions. It can also pick up an
   orphaned upload that finished after its publication timed out.

There is one operational manifest per stream. Its pending entries store IDs
and lifecycle states, and disappear once cleanup completes. Reject buckets
with enabled or suspended versioning. Use a private backend IAM role and
keep administrative bucket/lifecycle mutations out of the read and
publication path. Runtime CAS conflicts are normal. After an ambiguous
transport outcome during activation, resolve the active generation before
reporting final publication status.

Reader and publisher bounds are recorded in the manifest and must agree
across processes. A reader starts its window before fetching the manifest.
Every remote read and every final disclosure, including cache hits, is
fenced by that window. The reaper observes retired generations for twice the
reader window, and interrupted publications for twice the publisher window.
The reaper measures local elapsed time from a linearizable manifest
observation. Reader expiration uses both a monotonic deadline and a wall-time
backstop for system suspend. Deploy this profile against strongly consistent
manifest reads and clocks that track elapsed time. Opening a fresh snapshot
is required when the existing window expires. There is no automatic cleanup
worker in the library. The backend holding the namespace must schedule the
observer and sweeper.

## Sparse access

The adapter sends an exact HTTP range and `If-Match` pinned to the manifest
artifact ETag. It validates the response ETag, `Content-Range`, declared
length and streamed byte count. It rejects a full response to a ranged
request. The reader authenticates each touched fragment against the root
from the manifest. A lost range, timeout, or invalid fragment produces the
same unavailable query result as an access refusal.

The RAM and optional SSD caches have byte and entry limits. Keys include the
generation and expected content hashes, and entries contain only
authenticated fragments. SSD entries use anonymous temporary files and do
not persist across process restart. Cache hits still pass through the
phase-four live SpiceDB, Cedar and IFC gate. The reader enforces tenant scope
before the gate. Raw data and verified storage metadata stay within backend
code, including range metrics.

The optional batched strategy overlaps independent dependency index paths
and merges exactly adjacent authenticated ranges. The batch width is at
most eight. The reader waits to fetch any projected symbol until current
permissions have passed. Phase-five disclosure rules for links, discovery
and projections apply equally to the remote reader.

## Reproduction

```sh
cargo test --locked -p galadril-ontology --features remote --test remote -- --ignored
cargo test --locked -p galadril-ontology --features remote --test archive_spicedb docker_rustfs_spicedb -- --ignored
cargo test --locked -p galadril-ontology --features remote --lib http_incomplete_or_non_range -- --ignored
cargo test --locked -p galadril-ontology --features remote --test archive_spicedb docker_rustfs_benchmark -- --ignored --nocapture
bazel test //platform/ontology/rust:all
bazel test //platform/ontology/rust:remote_security_test
```

The Docker tests use RustFS 1.0.1 pinned to the digest already deployed by
Galadril and SpiceDB v1.56.2. The benchmark compares whole download, uncached
ranges, warm RAM cache and grouped/parallel uncached ranges, with 24 samples
per size and strategy. It reports p50/p95/p99 for open, a derived-view lookup
and a reference lookup. S3 counts include manifest GETs and response payload
bytes. SDK retries are disabled in the fixture. CPU and RSS are sampled on
the client process. The separately timed in-memory authority gate includes
SpiceDB, Cedar and IFC.

## Local RustFS benchmark

The following run used loopback on macOS arm64 with a Docker VM and the
unoptimized Cargo test profile. Each row represents 24 open, view and
link queries. Timing triplets are p50/p95/p99 in milliseconds. Counts and
bytes are totals. RSS readings include retained allocations from earlier
rows in the same process and do not estimate isolated peak memory.

| Entities | Strategy | Open ms | View ms | Links ms | S3 GETs | Response bytes | Client CPU ms | Client RSS KiB before → after |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 640 | Full archive | 16.76/27.43/27.64 | 9.40/13.32/14.39 | 3.63/9.41/15.73 | 48 | 16,139,712 | 500 | 48,176 → 50,096 |
| 640 | Uncached ranges | 41.75/66.75/114.56 | 84.92/121.09/122.69 | 86.97/132.98/135.04 | 1,104 | 2,287,872 | 2,410 | 50,096 → 50,240 |
| 640 | RAM cache | 13.89/23.07/27.51 | 13.90/23.21/28.11 | 4.96/9.37/11.18 | 40 | 33,976 | 490 | 50,624 → 50,640 |
| 640 | Grouped/parallel ranges | 43.37/68.64/71.72 | 55.85/78.33/84.26 | 67.24/110.15/143.09 | 1,104 | 2,287,872 | 1,930 | 50,656 → 50,640 |
| 3,200 | Full archive | 22.96/30.31/34.40 | 11.30/24.01/24.47 | 3.54/6.10/9.77 | 48 | 75,240,648 | 570 | 79,072 → 83,424 |
| 3,200 | Uncached ranges | 50.15/68.80/69.66 | 87.68/120.94/122.10 | 98.38/137.03/137.39 | 1,104 | 2,571,336 | 2,600 | 83,424 → 80,368 |
| 3,200 | RAM cache | 13.60/18.80/22.90 | 13.78/26.21/27.06 | 5.81/9.10/10.32 | 41 | 39,784 | 480 | 80,368 → 80,368 |
| 3,200 | Grouped/parallel ranges | 40.12/47.41/60.45 | 54.16/74.25/77.88 | 69.47/99.42/107.57 | 1,104 | 2,571,336 | 1,890 | 80,368 → 80,384 |

Archive sizes were 672,132 and 3,134,620 bytes. The phase-five local point
lookup on the first archive read 20,776 bytes including the initial open
and 804 bytes when warm. The remote benchmark used a derived view and an
additional intermodule link lookup, so their byte counts have different
query scopes.

The combined authority gate measured 6.81/14.55/21.83 ms and
5.21/13.22/14.98 ms. A burst of four simultaneous view queries on the
3,200-entity archive completed in 60.30 ms for sequential ranges,
22.08 ms for warm RAM, and 67.61 ms for parallel ranges. RAM hit rates
were 98.5% and 98.4%. The adjacent-range verifier has a dedicated test.
The selected view did not offer mergeable ranges. Overlap reduced
individual uncached view latency while the HTTP counts stayed constant.
Sequential sparse reads with a bounded RAM cache are the default.
Parallelism and SSD are configurable. These loopback results do not
establish WAN latency or production throughput.
