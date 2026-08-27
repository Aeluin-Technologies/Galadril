-- Applies tenant isolation and least-privilege grants to Vision tables.

DO $rls$
DECLARE
    protected_table TEXT;
BEGIN
    FOREACH protected_table IN ARRAY ARRAY[
        'entity_embeddings', 'eskg_events', 'entity_states',
        'causal_runs', 'pipeline_executions', 'authz_outbox',
        'identity_links'
    ] LOOP
        EXECUTE format(
            'ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY',
            protected_table
        );
        EXECUTE format(
            'ALTER TABLE public.%I FORCE ROW LEVEL SECURITY',
            protected_table
        );
        EXECUTE format(
            'DROP POLICY IF EXISTS tenant_isolation ON public.%I',
            protected_table
        );
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON public.%I FOR ALL '
            'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')) '
            'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), ''''))',
            protected_table
        );
        EXECUTE format(
            'REVOKE ALL ON public.%I FROM PUBLIC', protected_table
        );
        EXECUTE format(
            'GRANT SELECT, INSERT, UPDATE, DELETE ON public.%I TO galadril_app',
            protected_table
        );
    END LOOP;
END
$rls$;

REVOKE ALL ON SEQUENCE authz_outbox_id_seq FROM PUBLIC;
GRANT USAGE, SELECT ON SEQUENCE authz_outbox_id_seq TO galadril_app;

GRANT SELECT, UPDATE, DELETE ON authz_outbox TO galadril_maintenance;
GRANT USAGE, SELECT ON SEQUENCE authz_outbox_id_seq
TO galadril_maintenance;
