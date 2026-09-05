# PostgreSQL migrations 🐘

`gateway_migrations/` is Gateway's canonical SQL migration history.

The database images provision `galadril_app` as `NOSUPERUSER NOBYPASSRLS` with
`CREATE` on the application schema. Gateway applies these migrations with that
same constrained identity through its embedded SQLx migrator during startup.

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
