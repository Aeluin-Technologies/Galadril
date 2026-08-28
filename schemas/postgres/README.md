# PostgreSQL migrations

`gateway_migrations/` is Gateway's canonical SQLx migration history. The
timestamp prefix defines execution order; an applied file is immutable and
later changes require a new migration.

Galadril has not been deployed to production, so this initial series describes
the current fresh schema directly. Development databases may be dropped and
recreated instead of carrying compatibility migrations for older prototypes.

The database images provision `galadril_app` as `NOSUPERUSER NOBYPASSRLS` with
`CREATE` on the application schema. Gateway applies these migrations with that
same constrained identity through its embedded SQLx migrator during startup.

Vision uses SQLAlchemy 2 and GeoAlchemy2 `create_all` for its mapped tables.
Ontology and Vision load packaged, idempotent SQL resources for extensions,
Ontology persistence, RLS, grants, immutable-history triggers, and other DDL
that is not represented safely by ORM metadata. AGE graph creation remains a
runtime operation because its graph name is deployment configuration.

Gateway starts only when its connection cannot bypass RLS and every public
table containing a `tenant_id` column has enabled and forced RLS. The
application role may own the tables it creates because forced RLS applies to
table owners as well.

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
