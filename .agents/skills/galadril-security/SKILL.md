---
name: galadril-security
description: >-
  Use for Galadril authentication, authorization, tenant isolation, RLS,
  asynchronous identity propagation, retrieval/tool permissions or a
  security-focused code review. Not for generic formatting or ordinary
  bugfixes outside a trust boundary.
---
# Galadril security boundary review

Use `../../references/GALADRIL_CONTRACTS.md` to locate the current contracts.
First map the relevant entry points with `$galadril-context-audit` if unknown.

## Boundary checks

1. Identify the *authenticated* actor, verified tenant, workload identity,
   delegated authority and resource. Client headers, IDs, Kafka payloads and
   model output are never authorization proof. Do not equate mTLS with
   object-level authorization.
2. Inspect Gateway's SpiceDB permission decision and any additional Cedar
   restriction; reject malformed, absent or stale context as required.
   Re-authorize revocable/destructive deferred actions at execution time.
3. For PostgreSQL tenant work, check RLS (`USING` and `WITH CHECK`), normal
   non-`BYPASSRLS` roles and transaction-local `SET LOCAL app.tenant_id`;
   examine pooled connection reuse and missing-context behavior.
4. Check tenant scoping of cache, object storage, graph/vector retrieval,
   queues, idempotency, authorization outbox and model/RAG context. Distinguish
   intentional global resources from tenant-owned resources.
5. Check Registry's server-owned storage mapping and immutable revisions;
   private lakeFS/S3 credentials must not cross public API boundaries.
6. For Scribe and plugin tools, distinguish catalog discovery from permission
   to execute. Check delegated scopes, approval before side effects, safe
   treatment of retrieved text, and failure-closed behavior.
7. Verify structured security audit evidence without tokens, prompts, document
   bodies or other sensitive payloads.

## Required tests and report

Where relevant, test two tenants, a valid foreign resource ID, absent/forged
context, deny, timeout, revocation, retries, cache/pool reuse and cross-tenant
search. Start with the repository's existing security Bazel targets; do not
invent their coverage. Report confirmed findings and unresolved assumptions
with `path:line` evidence and severity only when demonstrable.

Never weaken isolation to make a test pass, or execute destructive security
experiments against live infrastructure without approval.
