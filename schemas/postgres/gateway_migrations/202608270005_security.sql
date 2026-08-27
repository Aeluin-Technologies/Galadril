-- Enforces immutable Gateway history, tenant RLS, and least privilege.

CREATE OR REPLACE FUNCTION reject_audit_event_mutation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit events are immutable';
END;
$$;

DROP TRIGGER IF EXISTS audit_events_immutable ON audit_events;
CREATE TRIGGER audit_events_immutable
BEFORE UPDATE OR DELETE ON audit_events
FOR EACH ROW EXECUTE FUNCTION reject_audit_event_mutation();

CREATE OR REPLACE FUNCTION reject_control_plane_history_mutation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'control-plane history is immutable';
END;
$$;

DROP TRIGGER IF EXISTS conversation_message_revisions_immutable
ON conversation_message_revisions;
CREATE TRIGGER conversation_message_revisions_immutable
BEFORE UPDATE OR DELETE ON conversation_message_revisions
FOR EACH ROW EXECUTE FUNCTION reject_control_plane_history_mutation();

DROP TRIGGER IF EXISTS pipeline_revisions_immutable ON pipeline_revisions;
CREATE TRIGGER pipeline_revisions_immutable
BEFORE UPDATE OR DELETE ON pipeline_revisions
FOR EACH ROW EXECUTE FUNCTION reject_control_plane_history_mutation();

DO $rls$
DECLARE
    tenant_table TEXT;
BEGIN
    FOREACH tenant_table IN ARRAY ARRAY[
        'iam_users', 'iam_roles', 'iam_user_roles', 'auth_policies',
        'audit_events', 'conversations', 'conversation_messages',
        'conversation_message_revisions',
        'conversation_message_attachments', 'pipeline_definitions',
        'pipeline_revisions'
    ] LOOP
        EXECUTE format(
            'ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', tenant_table
        );
        EXECUTE format(
            'ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', tenant_table
        );
        EXECUTE format(
            'DROP POLICY IF EXISTS tenant_isolation ON public.%I', tenant_table
        );
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON public.%I FOR ALL '
            'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')) '
            'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), ''''))',
            tenant_table
        );
        EXECUTE format(
            'REVOKE ALL ON public.%I FROM PUBLIC', tenant_table
        );
        IF tenant_table IN (
            'audit_events', 'conversation_message_revisions',
            'pipeline_revisions'
        ) THEN
            EXECUTE format(
                'GRANT SELECT, INSERT ON public.%I TO galadril_app',
                tenant_table
            );
        ELSE
            EXECUTE format(
                'GRANT SELECT, INSERT, UPDATE, DELETE ON public.%I TO galadril_app',
                tenant_table
            );
        END IF;
    END LOOP;
END
$rls$;
