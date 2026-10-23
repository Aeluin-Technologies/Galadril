-- Keep administrator labels independent of identity-provider profile refreshes.
ALTER TABLE iam_users ADD COLUMN display_name_override TEXT
    CHECK (display_name_override IS NULL OR char_length(display_name_override) BETWEEN 1 AND 128);
ALTER TABLE iam_roles ADD COLUMN display_name TEXT
    CHECK (display_name IS NULL OR char_length(display_name) BETWEEN 1 AND 128);
