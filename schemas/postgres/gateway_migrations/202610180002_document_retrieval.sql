-- Evidence shares the authoritative file record; graph relationships remain derived.
CREATE TABLE IF NOT EXISTS retrieval_units (
    tenant_id TEXT NOT NULL,
    source_kind TEXT NOT NULL DEFAULT 'file' CHECK (source_kind = 'file'),
    source_id TEXT NOT NULL,
    source_checksum TEXT NOT NULL,
    revision TEXT NOT NULL CHECK (revision ~ '^[a-f0-9]{32}$'),
    id TEXT NOT NULL CHECK (id ~ '^[a-f0-9]{64}$'),
    ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 0 AND 2047),
    text TEXT NOT NULL CHECK (octet_length(text) BETWEEN 1 AND 16384),
    location JSONB NOT NULL CHECK (jsonb_typeof(location) = 'object' AND octet_length(location::text) <= 1024),
    facts JSONB NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(facts) = 'array' AND jsonb_array_length(facts) <= 8 AND octet_length(facts::text) <= 32768),
    phrases TEXT[] NOT NULL DEFAULT '{}' CHECK (cardinality(phrases) <= 16),
    PRIMARY KEY (tenant_id, id, revision),
    UNIQUE (tenant_id, source_id, revision, ordinal),
    FOREIGN KEY (tenant_id, source_kind, source_id) REFERENCES pengolo_records(tenant_id, kind, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS retrieval_units_source ON retrieval_units (tenant_id, source_id, revision);
CREATE INDEX IF NOT EXISTS retrieval_units_phrases ON retrieval_units USING gin(phrases);

CREATE TABLE IF NOT EXISTS retrieval_features (
    tenant_id TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN -1 AND 7),
    profile_id TEXT NOT NULL CHECK (profile_id ~ '^[a-f0-9]{64}$'),
    embedding REAL[] NOT NULL CHECK (cardinality(embedding) BETWEEN 1 AND 4096 AND array_ndims(embedding) = 1 AND array_position(embedding, NULL) IS NULL AND NOT embedding && ARRAY['NaN'::real, 'Infinity'::real, '-Infinity'::real]),
    dimensions INTEGER GENERATED ALWAYS AS (cardinality(embedding)) STORED,
    PRIMARY KEY (tenant_id, unit_id, revision, ordinal),
    FOREIGN KEY (tenant_id, unit_id, revision) REFERENCES retrieval_units(tenant_id, id, revision) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS retrieval_features_profile ON retrieval_features(tenant_id, profile_id, ordinal);

CREATE TABLE IF NOT EXISTS retrieval_jobs (
    tenant_id TEXT NOT NULL,
    source_kind TEXT NOT NULL DEFAULT 'file' CHECK (source_kind = 'file'),
    source_id TEXT NOT NULL,
    source_checksum TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 8),
    next_attempt BIGINT NOT NULL DEFAULT 0,
    lease TEXT,
    lease_expires BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, source_id),
    FOREIGN KEY (tenant_id, source_kind, source_id) REFERENCES pengolo_records(tenant_id, kind, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS retrieval_jobs_due ON retrieval_jobs(next_attempt) WHERE attempts < 8;

DO $security$
DECLARE relation TEXT;
BEGIN
    FOREACH relation IN ARRAY ARRAY['retrieval_units','retrieval_features','retrieval_jobs'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', relation);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', relation);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I',relation);
        EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true),'''')) WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true),''''))',relation);
        EXECUTE format('REVOKE ALL ON %I FROM PUBLIC',relation);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO galadril_app',relation);
    END LOOP;
END
$security$;
DROP POLICY IF EXISTS retrieval_job_inventory ON retrieval_jobs;
CREATE POLICY retrieval_job_inventory ON retrieval_jobs FOR SELECT USING (current_setting('app.retrieval_maintenance',true) = 'on');

CREATE OR REPLACE FUNCTION retrieval_cosine(left_vector real[], right_vector real[]) RETURNS double precision
LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE AS $$
    SELECT sum(a.value::float8*b.value::float8) / NULLIF(sqrt(sum(a.value::float8*a.value::float8)*sum(b.value::float8*b.value::float8)),0)
    FROM unnest(left_vector) WITH ORDINALITY a(value,position)
    JOIN unnest(right_vector) WITH ORDINALITY b(value,position) USING(position)
    WHERE cardinality(left_vector)=cardinality(right_vector)
$$;
REVOKE ALL ON FUNCTION retrieval_cosine(real[],real[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION retrieval_cosine(real[],real[]) TO galadril_app;

DO $indexes$
DECLARE dimension INTEGER;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_textsearch') THEN
        EXECUTE 'CREATE INDEX IF NOT EXISTS retrieval_units_bm25 ON retrieval_units USING bm25(text) WITH (text_config=''french'')';
    ELSE
        CREATE INDEX IF NOT EXISTS retrieval_units_lexical ON retrieval_units USING gin(to_tsvector('french',text));
    END IF;
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vectorscale') THEN
        FOREACH dimension IN ARRAY ARRAY[1024,2048,2560,4096] LOOP
            EXECUTE format('ALTER TABLE retrieval_features ADD COLUMN IF NOT EXISTS ann_%s vector(%s) GENERATED ALWAYS AS (CASE WHEN cardinality(embedding)=%s THEN embedding::vector(%s) END) STORED',dimension,dimension,dimension,dimension);
            EXECUTE format('CREATE INDEX IF NOT EXISTS retrieval_features_%s_diskann ON retrieval_features USING diskann(ann_%s vector_cosine_ops) WHERE dimensions = %s',dimension,dimension,dimension);
        END LOOP;
    END IF;
END
$indexes$;

-- Existing files enter the same durable queue; temporary policies exist only
-- inside the migration transaction and never survive application startup.
DROP POLICY IF EXISTS retrieval_migration_read ON pengolo_records;
CREATE POLICY retrieval_migration_read ON pengolo_records FOR SELECT USING (true);
DROP POLICY IF EXISTS retrieval_migration_write ON retrieval_jobs;
CREATE POLICY retrieval_migration_write ON retrieval_jobs FOR INSERT WITH CHECK (true);
INSERT INTO retrieval_jobs(tenant_id,source_id,source_checksum)
SELECT tenant_id,id,data->>'checksum' FROM pengolo_records
WHERE kind='file' AND data->>'status'='ready'
  AND expires_at > (extract(epoch FROM clock_timestamp())*1000)::bigint
  AND data->>'checksum' IS NOT NULL
ON CONFLICT DO NOTHING;
DROP POLICY retrieval_migration_read ON pengolo_records;
DROP POLICY retrieval_migration_write ON retrieval_jobs;
