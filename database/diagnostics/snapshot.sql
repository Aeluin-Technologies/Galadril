-- Publish counts and durations only, since query text and backend identities can identify tenants.
WITH sessions AS MATERIALIZED (
    SELECT state, wait_event_type, wait_event, xact_start, pid
    FROM pg_stat_activity
    WHERE datname = current_database() AND backend_type = 'client backend'
      AND pid <> pg_backend_pid()
),
waits AS (
    SELECT wait_event_type AS wait_type, wait_event, count(*) AS sessions
    FROM sessions
    WHERE wait_event_type IS NOT NULL AND state IS DISTINCT FROM 'idle'
    GROUP BY wait_event_type, wait_event
),
locks AS (
    SELECT locktype, mode, granted, count(*) AS locks
    FROM pg_locks
    WHERE pid IN (SELECT pid FROM sessions)
    GROUP BY locktype, mode, granted
)
SELECT json_build_object(
    'blocked_sessions', (SELECT count(*) FROM sessions
                         WHERE wait_event_type = 'Lock' AND cardinality(pg_blocking_pids(pid)) > 0),
    'longest_transaction_seconds', (SELECT max(extract(epoch FROM statement_timestamp() - xact_start)) FROM sessions),
    'waits', coalesce((SELECT json_agg(waits ORDER BY wait_type, wait_event) FROM waits), '[]'::json),
    'locks', coalesce((SELECT json_agg(locks ORDER BY locktype, mode, granted) FROM locks), '[]'::json),
    'transactions', (SELECT json_build_object('commits', xact_commit, 'rollbacks', xact_rollback,
                                              'deadlocks', deadlocks, 'conflicts', conflicts)
                     FROM pg_stat_database WHERE datname = current_database()),
    'io', (SELECT coalesce(json_agg(io ORDER BY backend_type, object, context), '[]'::json)
           FROM (SELECT backend_type, object, context, reads, read_bytes, read_time,
                        writes, write_bytes, write_time, hits, evictions, fsyncs, fsync_time
                 FROM pg_stat_io) AS io)
);
