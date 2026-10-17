-- Normalize filenames before filtering so scoped cursors never hide relevant files.
CREATE FUNCTION pengolo_filename_key(value TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
RETURN regexp_replace(
    normalize(replace(replace(lower(value), 'œ', 'oe'), 'æ', 'ae'), NFKD),
    U&'[\0300-\036f]', '', 'g'
);

CREATE FUNCTION pengolo_filename_matches(filename TEXT, query TEXT) RETURNS BOOLEAN
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
RETURN NOT EXISTS (
    SELECT 1 FROM regexp_split_to_table(pengolo_filename_key(query), '[^[:alnum:]]+') AS terms(term)
    WHERE term <> '' AND strpos(pengolo_filename_key(filename), term) = 0
);

-- Released database images provide pg_trgm; minimal PostgreSQL fixtures retain exact normalized search.
DO $migration$
BEGIN
    IF to_regprocedure('word_similarity(text,text)') IS NOT NULL THEN
        EXECUTE $definition$
            CREATE OR REPLACE FUNCTION pengolo_filename_matches(filename TEXT, query TEXT) RETURNS BOOLEAN
            LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $body$
                SELECT NOT EXISTS (
                    SELECT 1 FROM regexp_split_to_table(pengolo_filename_key(query), '[^[:alnum:]]+') AS terms(term)
                    WHERE term <> ''
                      AND strpos(pengolo_filename_key(filename), term) = 0
                      AND (length(term) < 5 OR word_similarity(term, pengolo_filename_key(filename)) < 0.3)
                )
            $body$
        $definition$;
    END IF;
END
$migration$;
