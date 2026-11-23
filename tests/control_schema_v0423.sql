CREATE TABLE IF NOT EXISTS client_credentials (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    capabilities_json TEXT NOT NULL,
    workspace_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS client_action_receipts (
    client_id TEXT NOT NULL
        REFERENCES client_credentials(id) ON DELETE CASCADE,
    action_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_kind TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'accepted'
        CHECK(status IN ('accepted', 'refused', 'observed', 'unknown')),
    phase TEXT NOT NULL DEFAULT 'accepted',
    instance_id TEXT NOT NULL,
    result_resource_id TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY(client_id, action_id)
);
CREATE INDEX IF NOT EXISTS idx_client_actions_time
    ON client_action_receipts(client_id, created_at DESC);
