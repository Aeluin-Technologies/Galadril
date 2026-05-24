-- Repair legacy eskg_events schema so Timescale hypertable creation works.

DO $$
DECLARE
    has_table boolean;
    has_event_time boolean;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = 'eskg_events'
    ) INTO has_table;

    IF NOT has_table THEN
        RETURN;
    END IF;

    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'eskg_events'
          AND column_name = 'event_time'
    ) INTO has_event_time;

    IF has_event_time THEN
        RETURN;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = 'eskg_events_legacy'
    ) THEN
        EXECUTE 'ALTER TABLE public.eskg_events RENAME TO eskg_events_legacy';
    ELSIF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = 'eskg_events_legacy_2'
    ) THEN
        EXECUTE 'ALTER TABLE public.eskg_events RENAME TO eskg_events_legacy_2';
    ELSE
        -- Last resort: drop the incompatible table to unblock migration.
        EXECUTE 'DROP TABLE public.eskg_events';
    END IF;
END
$$;
