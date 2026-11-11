---
name: galadril-legal-research
description: Use Galadril to find and verify French decisions and legislation, check the text applicable on a given date, or substantiate an argument in a legal brief.
---

Start from the legal proposition to check and the precise act, procedural stage, and consequence at issue. Search with short public legal terms. Keep private names, addresses, correspondence and dossier details out of search queries.

Read and apply the complete LibreJustice procedures bundled with this skill.

- [Recherche de jurisprudence](references/recherche-jurisprudence/procedure.md) covers opposing outcomes, reformulation, full-text reading, appeals, recency and the research log.
- [Recherche de normes](references/recherche-normes/procedure.md) covers legally relevant dates, version histories, source hierarchy and documentary pitfalls.

Use both procedures for a dispute involving the applicability of a provision. Read their linked references when the procedure directs you to them. The steps below cover official corroboration and the fallback when LibreJustice is unavailable.

1. Use Galadril `search` with `CETAT` for administrative decisions or `JURI` for judicial decisions. For French norms use `CODE_DATE` for codes or `LODA_DATE` for legislation with `at_date` set to the legally relevant date. `JORF` helps locate publication. Search by exact reference when known. Otherwise try the rule, its exception, and a separate query for contrary outcomes. Galadril combines Légifrance and LibreJustice where available.
2. Treat snippets and summaries as leads. Use `fetch` with the returned official ID. If a LibreJustice hit has no usable Légifrance ID, search by court, date and number to resolve it. Leave unresolved references out of the verified authorities. A provider outage is a coverage gap.
3. For a decision, read the relevant grounds and dispositif. Check the court, date, number, posture, applicable text and material facts. Record separately what the court held and what relief it granted. A court can find a defect and still reject the application on an independent ground.
4. Use `verify_citation` with the Légifrance ID, exact court, date, number and a passage copied from the official source. Link to the official source when a URL is available. Otherwise cite the verified court, date and number without inventing a URL. Attribute quotations to the exact source, and distinguish grounds, conclusions of a rapporteur public and doctrine. Verification of text and identity leaves the applicability analysis to you.
5. For norms, check the exact article and its version on the relevant date, including effective dates and transitional provisions. Use public web research for materials beyond the official collections, such as doctrine and missing public documents, and identify those sources separately.

Read [Corpus and verification](references/corpus.md) before making a legal
claim from search results. Apply its search log and final check to each
independent proposition. For a follow-up, preserve the verified sources and
record what the new searches add or change.
