# Multi-principal empirical pipeline scenario

## Purpose

Exercise Gateway upload, object storage, Intake, Vision, LI-ESKG, Amarth,
SpiceDB, and Gateway reads with independently verifiable data and permissions.
The test must distinguish complete *internal* analysis from what each caller
is allowed to learn. A caller's lack of access must never suppress ingestion or
analysis for other callers, and a global result must never be returned to a
limited caller merely because one of its inputs is visible.

## Data sources and provenance

| Use | Source | Selection and constraints |
| --- | --- | --- |
| Transaction and identity linkage | [UCI Online Retail](https://archive.ics.uci.edu/dataset/352/online%2Bretail), Daqing Chen, DOI [10.24432/C5BW33](https://doi.org/10.24432/C5BW33), CC BY 4.0 | Select a pinned, anonymized subset of real invoice lines with recurring `CustomerID`, different customers buying similar products, cancellations, and missing or ambiguous fields. Preserve source row IDs and original values in the fixture manifest. |
| Experimental causal effect | [Cambridge randomized shopping-label trial](https://www.repository.cam.ac.uk/items/1c78e090-e017-43f2-9d9d-a93fc5123605), Natasha Clarke et al., DOI [10.17863/CAM.104079](https://doi.org/10.17863/CAM.104079), CC BY 4.0 | Inspect the data dictionary and original database before extraction. Retain only randomized assignment and the prespecified primary shopping outcome needed for an independent exploratory arm contrast. Exclude participant-level personal attributes and identifiers. |

Pin the downloaded originals and committed, minimal extracts by SHA-256. Keep
the originals out of CI; tests read only checked-in extracts, need no network,
and carry attribution plus selection code. A source row is empirical evidence,
not a claim that every inferred relationship is causal. The randomized trial
provides a causal benchmark; the observational retail records do not.

Source inspection on 2026-10-01 found:

| Original | SHA-256 | Verified properties |
| --- | --- | --- |
| UCI `online+retail.zip` | `f5385cbb54bbebf7196389109c6b0621faab0c304e3702548165e71c84aede8b` | Among the first 50,000 data rows, 118 customers each have at least nine sales across two invoices and one cancellation. The first 24 eligible customer IDs in ascending order provide 216 sales and 24 cancellations. |
| Cambridge `LabellingPurchasing_OriginalDatabase_20230401.xlsx` | `af33885a0754f3023abd658c61f2d70d0d6f51b0f9d2c4ff9e866097a71ce8d4` | The `MainData` sheet has 101 included participants in randomized condition 1 and 104 in condition 6. The original dictionary defines condition 1 as image-and-text labels with calories and condition 6 as no label. The primary outcome is alcohol units selected. |

The checked-in `fixtures/retail.csv` and `fixtures/trial.csv` have SHA-256
digests `d750b1e3d256c78eacf7007d28fad3c14c9baa83500ba9c144dd3b8eea4de7df`
and `17df82e5083ae0d6faf3c6d9a18dbb189a5a9d02745c557ceb21fa9b9bbd9073`,
respectively. Regenerate them with `fixtures/extract_empirical.py` and the two
pinned source downloads. Trial extract IDs are new sequential identifiers, not
original participant IDs or source row numbers; this avoids joining the CI
fixture to participant attributes in the original workbook.

```bash
uv run --no-project --with openpyxl python tests/e2e/fixtures/extract_empirical.py online-retail.zip cambridge-shopping-trial.xlsx tests/e2e/fixtures
```

For conditions 1 and 6, the independent unadjusted mean difference is about
`+1.998` units with an approximate normal 95% interval of `[-3.831, 7.827]`.
Randomization makes this a causal *estimand*, but the interval includes zero:
the test must not report a proven nonzero benefit or harm. This deliberately
guards against labeling an uncertain association as established causality.
Keep only the outcome and assignment, not the original participant IDs, in
the extract.

## Principals and grants

The scenario has one tenant administrator, sales and returns stewards, a
cross-domain analyst, sales-only and returns-only readers, an exact-object
reader, an uploader without read access to another steward's objects, and an
unprivileged tenant member. The administrator creates principals and assigns
bounded roles through Gateway. The 36 initial Gateway uploads are distributed
among at least six principals; a principal may submit multiple files. Every
upload proves its own principal, S3 owner tag, raw ownership relationship, and
derived lineage.

| Principal | Sales | Returns | Selected object | IAM | Upload |
| --- | --- | --- | --- | --- | --- |
| Tenant administrator | All | All | All | Yes | Yes |
| Sales steward | Own and granted sales | None | As granted | No | Sales only |
| Returns steward | None | Own and granted returns | As granted | No | Returns only |
| Cross-domain analyst | Granted sales | Granted returns | As granted | No | No |
| Sales reader | Granted sales | None | As granted | No | No |
| Returns reader | None | Granted returns | As granted | No | No |
| Exact-object reader | None by role | None by role | One granted object | No | No |
| Unprivileged member | None | None | None | No | No |

Assertions cover every cell, including the union for the analyst, disjoint
steward views, exact-object exceptions, and revocation. Readers cannot request
staging uploads or promote objects. Stewards cannot create users or roles,
grant themselves permissions, or assign a role broader than their own. Denied
mutations must leave the directory, relationships, objects, and audit state
unchanged except for a denial record.

## Observation and identity oracle

Select 240 empirical retail rows representing 24 distinct customers: nine
sales across at least two invoices and one cancellation per customer. Package
them into the 36 Gateway uploads without changing the source values. Later add
a separately counted set of missing-ID rows as quarantine cases. The ingestion
parser must emit one
independently traceable observation per accepted row. The deterministic
inference fixture may replace heavy AI models, but must use stable identity
evidence from the row rather than a single cohort label. Include lookalike
customers, repeated invoices, and customers represented in both permission
domains.

Compute expected counts from a committed manifest **before** running the
pipeline: uploaded files, accepted rows, rejected rows, distinct source
customers, resolved identities, events, source edges, and observations per
identity. Compare the entire mapping and edge set, not only totals. For
ambiguous rows, declare the expected quarantine or non-merge behavior in the
manifest. Wait for durable completion and drained authorization outbox rather
than sleeping between uploads.

## Analysis and non-disclosure oracle

The service principal processes all authorized tenant inputs, irrespective of
the requesting user's view. An administrator can inspect the full result and
its source lineage. Limited callers see only explicitly authorized raw,
entity, event, relation, embedding, and causal-result projections. Every
returned edge must have visible endpoints and every returned value must derive
only from the caller's visible inputs, unless an explicit aggregate-release
policy permits disclosure. A shared entity built from sales and returns must
not expose the other steward's state or the global causal estimate.

Run Amarth over the 205 included, empirical randomized-trial observations and
compare its reported treatment effect with the independent unadjusted arm
contrast above using a predeclared numerical tolerance. Require treatment
assignment, outcome, sample count, finite effect size, uncertainty, and
provenance in the stored result. An observational retail association may be
reported as exploratory, but must not be asserted as a validated causal
effect. The trial's uncertain result must remain uncertain for limited and
administrative callers alike.

## Implementation boundaries

1. Add failing service-owned tests for scoped upload delegation, row-level
   lineage, mixed-ownership entity projections, and relation filtering before
   changing production code.
2. Add fixture validation tests for source checksums, licensing attribution,
   selected row mapping, and the independent randomized-trial estimate.
3. Keep the current lightweight lifecycle test. Split the new scenario into
   `scenario.py` (typed manifest and permission oracle), `empirical_data.py`
   (offline fixture loader), `security_matrix.py` (Gateway assertions), and
   `test_empirical_pipeline.py` (the full integration flow). Keep data and
   environment setup in dedicated fixture modules.
4. Run the Bazel support tests first, then the full container E2E target and
   `bazel test //...`. Report the actual CI-equivalent result; a local static
   check is not proof that the full pipeline ran.

## Current contract gaps

The canonical Intake CSV parser emits one observation per source row. Gateway
now delegates ingestion and raw reading by domain; its state and graph readers
authorize source events before releasing mixed-owner evidence. Vision persists
bounded scalar treatment and outcome evidence. The trial's known randomized
assignment DAG is analyzed by Amarth's DoWhy estimator over rows read back
from Vision persistence; the pre-existing rolling temporal causal job remains
reserved for time-series inference. The randomized estimate is currently
verified inside the E2E test but is not yet a public, permission-filtered
Gateway result. Do not expose that aggregate through a general entity grant:
it needs a separate release policy and durable result contract.
