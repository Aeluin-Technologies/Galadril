-- Enforces immutable ontology history, controlled publication, and tenant RLS.

CREATE OR REPLACE FUNCTION reject_ontology_revision_mutation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'ontology revisions are immutable';
END;
$$;

DROP TRIGGER IF EXISTS ontology_revisions_immutable ON ontology_revisions;
CREATE TRIGGER ontology_revisions_immutable
BEFORE UPDATE OR DELETE ON ontology_revisions
FOR EACH ROW EXECUTE FUNCTION reject_ontology_revision_mutation();

DROP TRIGGER IF EXISTS ontology_revision_parents_immutable
ON ontology_revision_parents;
CREATE TRIGGER ontology_revision_parents_immutable
BEFORE UPDATE OR DELETE ON ontology_revision_parents
FOR EACH ROW EXECUTE FUNCTION reject_ontology_revision_mutation();

DROP TRIGGER IF EXISTS ontology_base_artifacts_immutable
ON ontology_base_artifacts;
CREATE TRIGGER ontology_base_artifacts_immutable
BEFORE UPDATE OR DELETE ON ontology_base_artifacts
FOR EACH ROW EXECUTE FUNCTION reject_ontology_revision_mutation();

CREATE OR REPLACE FUNCTION reject_ontology_publication_rewrite()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.lifecycle <> 'production'
        OR NEW.lifecycle <> 'retired'
        OR NEW.retired_at IS NULL
        OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
        OR NEW.ontology_id IS DISTINCT FROM OLD.ontology_id
        OR NEW.publication_id IS DISTINCT FROM OLD.publication_id
        OR NEW.revision_id IS DISTINCT FROM OLD.revision_id
        OR NEW.metadata IS DISTINCT FROM OLD.metadata
        OR NEW.published_at IS DISTINCT FROM OLD.published_at
    THEN
        RAISE EXCEPTION 'ontology publication history is immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS ontology_publications_retirement_only
ON ontology_publications;
CREATE TRIGGER ontology_publications_retirement_only
BEFORE UPDATE ON ontology_publications
FOR EACH ROW EXECUTE FUNCTION reject_ontology_publication_rewrite();

CREATE OR REPLACE FUNCTION ontology_compare_and_swap_branch_head(
    requested_tenant TEXT,
    requested_branch TEXT,
    expected_head CHAR(32),
    replacement_head CHAR(32)
) RETURNS BOOLEAN LANGUAGE plpgsql AS $$
DECLARE
    changed_rows INTEGER;
BEGIN
    UPDATE ontology_branches
    SET head_revision_id = replacement_head, updated_at = NOW()
    WHERE tenant_id = requested_tenant
      AND branch_name = requested_branch
      AND head_revision_id IS NOT DISTINCT FROM expected_head;
    GET DIAGNOSTICS changed_rows = ROW_COUNT;
    RETURN changed_rows = 1;
END;
$$;

DO $rls$
DECLARE
    protected_table TEXT;
BEGIN
    FOREACH protected_table IN ARRAY ARRAY[
        'ontology_revisions', 'ontology_revision_parents',
        'ontology_branches', 'ontology_merge_conflicts',
        'ontology_materializations', 'ontology_catalog',
        'ontology_publications', 'pipeline_ontology_bindings'
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
    END LOOP;
END
$rls$;

REVOKE ALL ON ontology_base_artifacts FROM PUBLIC;
REVOKE UPDATE, DELETE ON ontology_base_artifacts FROM galadril_app;
GRANT SELECT, INSERT ON ontology_base_artifacts TO galadril_app;

GRANT SELECT, INSERT ON
    public.ontology_revisions,
    public.ontology_revision_parents,
    public.ontology_merge_conflicts,
    public.ontology_materializations
TO galadril_app;

GRANT SELECT, INSERT, UPDATE ON
    public.ontology_branches,
    public.ontology_catalog,
    public.ontology_publications,
    public.pipeline_ontology_bindings
TO galadril_app;
