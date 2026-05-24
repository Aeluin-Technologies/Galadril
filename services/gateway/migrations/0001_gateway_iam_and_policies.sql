-- Gateway-owned IAM + Cedar policy storage.

CREATE TABLE IF NOT EXISTS iam_users (
    tenant_id  TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    is_active  BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, user_id)
);

CREATE TABLE IF NOT EXISTS iam_roles (
    tenant_id  TEXT NOT NULL,
    role_name  TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, role_name)
);

CREATE TABLE IF NOT EXISTS iam_user_roles (
    tenant_id  TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    role_name  TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, user_id, role_name),
    FOREIGN KEY (tenant_id, user_id) REFERENCES iam_users(tenant_id, user_id)
        ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, role_name) REFERENCES iam_roles(tenant_id, role_name)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS iam_user_permissions (
    tenant_id  TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    effect     TEXT NOT NULL CHECK (effect IN ('allow', 'deny')),
    action     TEXT NOT NULL,
    scope      JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, user_id, effect, action, scope),
    FOREIGN KEY (tenant_id, user_id) REFERENCES iam_users(tenant_id, user_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS iam_role_permissions (
    tenant_id  TEXT NOT NULL,
    role_name  TEXT NOT NULL,
    effect     TEXT NOT NULL CHECK (effect IN ('allow', 'deny')),
    action     TEXT NOT NULL,
    scope      JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, role_name, effect, action, scope),
    FOREIGN KEY (tenant_id, role_name) REFERENCES iam_roles(tenant_id, role_name)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS auth_policies (
    tenant_id  TEXT NOT NULL,
    id         TEXT NOT NULL,
    content    TEXT NOT NULL,
    is_active  BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, id)
);
