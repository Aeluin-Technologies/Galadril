-- Creates authoritative ontology revision and runtime publication tables.

CREATE TABLE IF NOT EXISTS ontology_base_artifacts (
    base_version TEXT PRIMARY KEY,
    base_hash CHAR(64) NOT NULL,
    ontology JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (base_version, base_hash),
    CHECK (base_version <> ''),
    CHECK (base_hash ~ '^[0-9a-f]{64}$'),
    CHECK (jsonb_typeof(ontology) = 'object')
);

CREATE TABLE IF NOT EXISTS ontology_revisions (
    tenant_id TEXT NOT NULL,
    revision_id CHAR(32) NOT NULL,
    base_version TEXT NOT NULL,
    base_hash CHAR(64) NOT NULL,
    change_set JSONB NOT NULL DEFAULT '[]'::jsonb,
    author TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, revision_id),
    FOREIGN KEY (base_version, base_hash)
        REFERENCES ontology_base_artifacts (base_version, base_hash),
    CHECK (tenant_id <> ''),
    CHECK (jsonb_typeof(change_set) = 'array')
);

CREATE TABLE IF NOT EXISTS ontology_revision_parents (
    tenant_id TEXT NOT NULL,
    revision_id CHAR(32) NOT NULL,
    parent_revision_id CHAR(32) NOT NULL,
    parent_order SMALLINT NOT NULL,
    PRIMARY KEY (tenant_id, revision_id, parent_order),
    UNIQUE (tenant_id, revision_id, parent_revision_id),
    FOREIGN KEY (tenant_id, revision_id)
        REFERENCES ontology_revisions (tenant_id, revision_id),
    FOREIGN KEY (tenant_id, parent_revision_id)
        REFERENCES ontology_revisions (tenant_id, revision_id),
    CHECK (parent_order BETWEEN 0 AND 1),
    CHECK (revision_id <> parent_revision_id)
);

CREATE TABLE IF NOT EXISTS ontology_branches (
    tenant_id TEXT NOT NULL,
    branch_name TEXT NOT NULL,
    head_revision_id CHAR(32) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, branch_name),
    FOREIGN KEY (tenant_id, head_revision_id)
        REFERENCES ontology_revisions (tenant_id, revision_id),
    CHECK (tenant_id <> ''),
    CHECK (branch_name <> '')
);

CREATE TABLE IF NOT EXISTS ontology_merge_conflicts (
    tenant_id TEXT NOT NULL,
    merge_id CHAR(32) NOT NULL,
    conflict_id CHAR(32) NOT NULL,
    conflict_order INTEGER NOT NULL,
    conflict JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, merge_id, conflict_id),
    UNIQUE (tenant_id, merge_id, conflict_order),
    CHECK (jsonb_typeof(conflict) = 'object')
);

CREATE TABLE IF NOT EXISTS ontology_materializations (
    tenant_id TEXT NOT NULL,
    revision_id CHAR(32) NOT NULL,
    base_version TEXT NOT NULL,
    base_hash CHAR(64) NOT NULL,
    effective_hash CHAR(64) NOT NULL,
    overlay JSONB NOT NULL,
    effective_ontology JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, revision_id),
    FOREIGN KEY (tenant_id, revision_id)
        REFERENCES ontology_revisions (tenant_id, revision_id) ON DELETE CASCADE,
    CHECK (jsonb_typeof(overlay) = 'object'),
    CHECK (jsonb_typeof(effective_ontology) = 'object')
);

CREATE TABLE IF NOT EXISTS ontology_catalog (
    tenant_id TEXT NOT NULL,
    ontology_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, ontology_id),
    CHECK (tenant_id <> ''),
    CHECK (ontology_id <> ''),
    CHECK (display_name <> '')
);

CREATE TABLE IF NOT EXISTS ontology_publications (
    tenant_id TEXT NOT NULL,
    ontology_id TEXT NOT NULL,
    publication_id CHAR(32) NOT NULL,
    revision_id CHAR(32) NOT NULL,
    lifecycle TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    published_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    retired_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, ontology_id, publication_id),
    UNIQUE (tenant_id, ontology_id, publication_id),
    FOREIGN KEY (tenant_id, ontology_id)
        REFERENCES ontology_catalog (tenant_id, ontology_id),
    FOREIGN KEY (tenant_id, revision_id)
        REFERENCES ontology_revisions (tenant_id, revision_id),
    CHECK (lifecycle IN ('production', 'retired')),
    CHECK (jsonb_typeof(metadata) = 'object')
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_ontology_publication_production
ON ontology_publications (tenant_id, ontology_id)
WHERE lifecycle = 'production';

CREATE TABLE IF NOT EXISTS pipeline_ontology_bindings (
    tenant_id TEXT NOT NULL,
    pipeline_id TEXT NOT NULL,
    block_id TEXT NOT NULL,
    ontology_id TEXT NOT NULL,
    resource_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    resource_kinds JSONB NOT NULL DEFAULT '[]'::jsonb,
    include_dependencies BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, pipeline_id, block_id),
    FOREIGN KEY (tenant_id, ontology_id)
        REFERENCES ontology_catalog (tenant_id, ontology_id),
    CHECK (pipeline_id <> ''),
    CHECK (block_id <> ''),
    CHECK (jsonb_typeof(resource_ids) = 'array'),
    CHECK (jsonb_typeof(resource_kinds) = 'array'),
    CHECK (jsonb_array_length(resource_ids) > 0
        OR jsonb_array_length(resource_kinds) > 0),
    CHECK (jsonb_typeof(metadata) = 'object')
);

CREATE INDEX IF NOT EXISTS idx_ontology_parent_lookup
ON ontology_revision_parents (
    tenant_id, parent_revision_id, revision_id
);
