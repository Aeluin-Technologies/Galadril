-- A transaction can confirm the complete candidate set without rereading
-- private embeddings or receipts under the publication lock.
ALTER TABLE public.identity_prototypes
    ADD COLUMN IF NOT EXISTS prototype_revision UUID NOT NULL DEFAULT gen_random_uuid();

CREATE OR REPLACE FUNCTION public.rotate_identity_prototype_revision()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    NEW.prototype_revision := gen_random_uuid();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS identity_prototype_revision ON public.identity_prototypes;
CREATE TRIGGER identity_prototype_revision
    BEFORE INSERT OR UPDATE ON public.identity_prototypes
    FOR EACH ROW EXECUTE FUNCTION public.rotate_identity_prototype_revision();

CREATE INDEX IF NOT EXISTS idx_identity_prototypes_signed_domain
    ON public.identity_prototypes (tenant_id, modality, entity_id)
    INCLUDE (prototype_revision)
    WHERE gol_value IS NOT NULL AND gol_receipt IS NOT NULL;
