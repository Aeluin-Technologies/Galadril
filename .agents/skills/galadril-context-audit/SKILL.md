---
name: galadril-context-audit
description: >-
  Use before an unfamiliar Galadril architecture review, threat model or
  cross-service audit to map assumptions, guarantees, callers and boundaries.
  This is discovery only, not vulnerability triage or implementation.
---
# Evidence-first context audit

## Scope

1. Identify the requested module, entry points and authoritative documentation.
   Consult `../../references/GALADRIL_CONTRACTS.md` selectively.
2. Trace actual call paths, including failure, timeout, retry and asynchronous
   paths. For each material boundary, record the inputs, outputs, state owner,
   invariants, trust level and dependent calls.
3. Separate what code *enforces* from what comments, function names, external
   proxies or configuration merely *assume*. Verify downstream guarantees
   at their implementation, not by name.
4. Map tenant identity, authorization, immutable revision, event provenance,
   transaction and telemetry propagation where relevant. Mark unknown
   implementations or external services explicitly.
5. Identify conflicts between tests, documentation and behavior without
   silently reconciling them. Do not treat an architecture document describing
   a planned service as evidence it is currently deployed.

## Deliverable

- Concise boundary map and call chain, each substantive assertion backed by
  `path:line` evidence.
- Table or compact notes with `guaranteed`, `assumed`, `not found`, `unknown`.
- Open questions and the next files or checks needed.
- No vulnerability severity ratings, speculative exploits or unsolicited fixes.

Keep the analysis bounded to the requested area; use external files only when
they materially help. Do not assume agent delegation or a vendor-specific
command is available. If a later security assessment is requested, hand its
evidence to `$galadril-security`.
