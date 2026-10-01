-- Preserve distinct observations of one entity at the same source event time.

DO $entity_state_pk$
DECLARE
    constraint_row pg_constraint%ROWTYPE;
    key_columns TEXT[];
BEGIN
    SELECT * INTO constraint_row
    FROM pg_constraint
    WHERE conrelid = 'public.entity_states'::regclass
      AND contype = 'p';

    IF NOT FOUND THEN
        RAISE EXCEPTION 'entity_states primary key is missing';
    END IF;

    SELECT array_agg(attribute.attname ORDER BY key_column.ordinality)
    INTO key_columns
    FROM unnest(constraint_row.conkey) WITH ORDINALITY
        AS key_column(attnum, ordinality)
    JOIN pg_attribute AS attribute
      ON attribute.attrelid = constraint_row.conrelid
     AND attribute.attnum = key_column.attnum;

    IF key_columns = ARRAY['tenant_id', 'entity_id', 'event_id', 'event_time'] THEN
        RETURN;
    END IF;
    IF key_columns IS DISTINCT FROM ARRAY['tenant_id', 'entity_id', 'event_time'] THEN
        RAISE EXCEPTION 'unexpected entity_states primary key: %', key_columns;
    END IF;

    EXECUTE format(
        'ALTER TABLE public.entity_states DROP CONSTRAINT %I',
        constraint_row.conname
    );
    ALTER TABLE public.entity_states
        ADD PRIMARY KEY (tenant_id, entity_id, event_id, event_time);
END
$entity_state_pk$;
