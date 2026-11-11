# Corpus and verification

## Define the question

Write one sentence per proposition. Identify the court or order, the act or
contract at issue, the stage of proceedings, the desired relief and the date
that controls the applicable law. Treat the user's legal premise as a claim
to test. Distinguish facts known from evidence, facts merely asserted, and
facts that would change the answer if established.

For a procedural defect, research the existence of the requirement, whether
it was breached, and the consequence separately. For example, a finding
that a rule was inopposable does not establish that an ajournement was
annulled. Read the dispositive articles and look for an independent ground
that explains the outcome.

## Build a query set for Galadril

| Purpose | Example `search` arguments |
| --- | --- |
| Known docket | `{"query":"335033","fund":"CETAT"}` |
| Candidate rule | `{"query":"vice de procédure privation d'une garantie","fund":"CETAT"}` |
| Contrary outcome | `{"query":"vice de procédure sans incidence motif autonome rejet","fund":"CETAT"}` |
| Version of a code | `{"query":"code de justice administrative L. 521-1","fund":"CODE_DATE","at_date":"2025-06-01"}` |

These calls illustrate the wire contract, not verified answers. Search the
exact formula and a descriptive reformulation. Search the claimed rule and
a distinct exception or rejection theory. Do not start with an arbitrary
recent-date cutoff when looking for a foundational decision. For exhaustive
coverage, use authorized sources that expose the required corpus filters.

Galadril's search accepts `query`, `provider`, `fund`, `at_date` and `page`.
It does not expose LibreJustice's native outcome, seat, facet or sort
parameters. The shortlist is not a count of the corpus or a representative
sample of outcomes. A hit's rank, snippet and summary cannot establish
direction, absence of precedent, or how common a holding is. If the
shortlists are thin, refine the wording, use additional pages where
supported, and state the residual coverage gap.

## Read before using

For each candidate, store its provenance and official identifier. Fetch
the official text. A LibreJustice ID that the official service cannot
resolve is a lead. Resolve it by court, date and docket. Check any relevant
appeal or cassation by searching the docket and related identifiers. A
search without a match does not prove finality.

Read the context surrounding the relevant passage. Classify it as a party's
submission, a court finding, a legal rule, an application to the facts or
part of the dispositif. Keep commentaries and rapporteur conclusions
separate. If a sentence says « cette mention » or « ce délai », identify
the exact referent in the preceding discussion before treating the case as
authority. An analogy involving another object or remedy must be labelled
as such.

For a decision quote, send `verify_citation` a `citation` object with
`provider: "legifrance"`, the official `identifier`, `court`, `date`,
`number` and the literal `passage`. Use one continuous passage per quote.
If verification fails, compare the official text and the metadata before
retrying. Omit unverifiable quotations. For an article, check the article's
identity, full text and applicable version against the official record.
The decision verification endpoint does not replace that temporal check.

## Keep evidence reproducible

Keep a working ledger with the query and filters actually sent, the
provider status, the official ID and passages actually opened, and the
reason a candidate supports, contradicts or does not answer the proposition.
Preserve decisive passages verbatim and summarize discarded leads. If work
resumes, keep the ledger tied to the same question and recheck a source
whose version or subsequent history matters.

In the answer, give the conclusion at the precision the sources support.
For each relied-on authority, give its court, date, number, official link
when available, the relevant passage or a clearly attributed paraphrase,
and why the facts and procedural posture make it applicable. Include the
strongest contrary authority found and any uncertainty that changes the
answer. Remove every citation that cannot be tied to an official text
actually read in this investigation.
