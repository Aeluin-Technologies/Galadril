-- Repair legacy eskg_events primary key for TimescaleDB hypertables.

DO $$
DECLARE
    pk_name text;
    pk_cols text;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = 'eskg_events'
    ) THEN
        RETURN;
    END IF;

    -- If event_time doesn't exist, schema repair must run first.
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'eskg_events'
          AND column_name = 'event_time'
    ) THEN
        RETURN;
    END IF;

    SELECT c.conname
    INTO pk_name
    FROM pg_constraint c
    JOIN pg_class t ON t.oid = c.conrelid
    WHERE t.relname = 'eskg_events'
      AND c.contype = 'p'
    LIMIT 1;

    IF pk_name IS NOT NULL THEN
        SELECT string_agg(a.attname, ',' ORDER BY ck.ord)
        INTO pk_cols
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN unnest(c.conkey) WITH ORDINALITY AS ck(attnum, ord) ON TRUE
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ck.attnum
        WHERE t.relname = 'eskg_events'
          AND c.contype = 'p'
        GROUP BY c.conname;

        IF pk_cols IS NOT NULL AND position('event_time' in pk_cols) = 0 THEN
            EXECUTE format('ALTER TABLE public.eskg_events DROP CONSTRAINT %I', pk_name);
        END IF;
    END IF;

    -- Ensure composite PK exists.
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = 'eskg_events'
          AND c.contype = 'p'
    ) THEN
        EXECUTE 'ALTER TABLE public.eskg_events ADD CONSTRAINT eskg_events_pkey PRIMARY KEY (event_id, event_time)';
    END IF;
END
$$;
