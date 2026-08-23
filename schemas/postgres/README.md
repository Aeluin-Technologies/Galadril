# Gateway PostgreSQL schema

`gateway.sql` is the complete current Gateway-owned schema for a fresh
environment. There is no compatibility or historical upgrade layer because no
previous Gateway schema was deployed.

Provision the non-owner `galadril_app` role first, then apply `gateway.sql` as
the database owner. Gateway itself never changes schema. It starts only when
its connection is a normal non-owner role and every public table containing a
`tenant_id` column has enabled and forced RLS.

Every new tenant table must:

1. contain a non-null `tenant_id` in its primary or unique identity;
2. enable and force RLS;
3. define both `USING` and `WITH CHECK` against
   `NULLIF(current_setting('app.tenant_id', true), '')`;
4. revoke public access and grant only required operations to `galadril_app`;
5. have real Postgres tests for missing context, two-tenant reads and writes,
   concurrent pool use, rollback, and connection reuse.

Application code obtains tenant data only through `Database::tenant`, which
begins a transaction and installs `SET LOCAL` context before returning it.
Global data must live in a table without `tenant_id` and is queried explicitly
through `Database::system`; this path cannot see tenant-owned rows through the
normal application role.
