-- Commit-delivered invalidations cover indexing workers and all Gateway replicas.
-- Only scope identifiers cross this internal channel; clients re-read through IAM.
CREATE FUNCTION notify_pengolo_library() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'INSERT' AND OLD.kind = 'file' THEN
        PERFORM pg_notify('pengolo_library', json_build_object(
            'tenant', OLD.tenant_id, 'owner', OLD.owner_id, 'project', OLD.project_id
        )::text);
    END IF;
    IF TG_OP <> 'DELETE' AND NEW.kind = 'file' THEN
        PERFORM pg_notify('pengolo_library', json_build_object(
            'tenant', NEW.tenant_id, 'owner', NEW.owner_id, 'project', NEW.project_id
        )::text);
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER pengolo_library_changes
AFTER INSERT OR UPDATE OR DELETE ON pengolo_records
FOR EACH ROW EXECUTE FUNCTION notify_pengolo_library();
