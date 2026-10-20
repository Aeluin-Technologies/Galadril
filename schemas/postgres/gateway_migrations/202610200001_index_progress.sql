-- Keep transient processing progress on the existing leased projection job.
ALTER TABLE retrieval_jobs ADD COLUMN progress JSONB NOT NULL
    DEFAULT '{"stage":"queued","percent":0}'::jsonb
    CHECK ((jsonb_typeof(progress) = 'object'
        AND progress->>'stage' IN ('queued','reading','parsing','extracting','embedding','publishing')
        AND jsonb_typeof(progress->'percent') = 'number'
        AND (progress->>'percent')::integer BETWEEN 0 AND 99) IS TRUE);
