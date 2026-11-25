-- Existing unlabelled prototypes remain ineligible for security-aware resolution.
ALTER TABLE public.identity_prototypes
    ADD COLUMN IF NOT EXISTS gol_value JSONB,
    ADD COLUMN IF NOT EXISTS gol_receipt TEXT;
