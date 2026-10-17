-- Shared matching preserves literal punctuation while reusing normalized trigram lookup.
CREATE FUNCTION search_normalized_key(value TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
RETURN pengolo_filename_key(value);

CREATE FUNCTION search_text_matches(value TEXT, query TEXT) RETURNS BOOLEAN
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
RETURN CASE WHEN query !~ '[[:alnum:]]'
    THEN strpos(search_normalized_key(value), search_normalized_key(query)) > 0
    ELSE pengolo_filename_matches(value, query)
END;
