-- Derived memory remains unusable until its canonical event is authorized.
CREATE TABLE IF NOT EXISTS pipeline_evidence (
    tenant_id TEXT NOT NULL,
    source_id TEXT NOT NULL CHECK (length(source_id) BETWEEN 1 AND 256),
    unit_id TEXT NOT NULL CHECK (unit_id ~ '^[a-f0-9]{64}$'),
    event_id TEXT NOT NULL CHECK (length(event_id) BETWEEN 1 AND 256),
    event_time TIMESTAMPTZ NOT NULL,
    checksum TEXT NOT NULL CHECK (checksum ~ '^[a-f0-9]{64}$'),
    text TEXT NOT NULL CHECK (octet_length(text) BETWEEN 1 AND 16384),
    metadata JSONB NOT NULL CHECK (jsonb_typeof(metadata)='object' AND octet_length(metadata::text)<=32768),
    facts JSONB NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(facts)='array' AND jsonb_array_length(facts)<=8 AND octet_length(facts::text)<=32768),
    profile_id TEXT NOT NULL CHECK (profile_id ~ '^[a-f0-9]{64}$'),
    embedding REAL[] NOT NULL CHECK (cardinality(embedding) BETWEEN 1 AND 4096 AND array_ndims(embedding)=1 AND array_position(embedding,NULL) IS NULL AND NOT embedding && ARRAY['NaN'::real,'Infinity'::real,'-Infinity'::real]),
    pipeline_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,source_id,unit_id)
);
CREATE INDEX IF NOT EXISTS pipeline_evidence_chronology ON pipeline_evidence(tenant_id,event_time,event_id);
CREATE INDEX IF NOT EXISTS pipeline_evidence_event ON pipeline_evidence(tenant_id,event_id);
CREATE INDEX IF NOT EXISTS pipeline_evidence_metadata ON pipeline_evidence USING gin(metadata);
CREATE INDEX IF NOT EXISTS pipeline_evidence_text ON pipeline_evidence USING gin(to_tsvector('french',text));
ALTER TABLE pipeline_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE pipeline_evidence FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON pipeline_evidence;
CREATE POLICY tenant_isolation ON pipeline_evidence USING (tenant_id=NULLIF(current_setting('app.tenant_id',true),'')) WITH CHECK (tenant_id=NULLIF(current_setting('app.tenant_id',true),''));
REVOKE ALL ON pipeline_evidence FROM PUBLIC;
GRANT SELECT,INSERT,UPDATE,DELETE ON pipeline_evidence TO galadril_app;
