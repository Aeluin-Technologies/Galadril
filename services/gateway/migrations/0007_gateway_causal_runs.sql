-- Gateway-owned causal run cache.

CREATE TABLE IF NOT EXISTS causal_runs (
    cache_key      TEXT PRIMARY KEY,
    target         TEXT NOT NULL,
    window_start   TIMESTAMPTZ NOT NULL,
    window_end     TIMESTAMPTZ NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status         TEXT NOT NULL,
    result_summary JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_causal_runs_window
ON causal_runs (window_start DESC, window_end DESC);

CREATE INDEX IF NOT EXISTS idx_causal_runs_target
ON causal_runs (target, created_at DESC);
