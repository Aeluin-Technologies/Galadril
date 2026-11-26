-- Rank by separate cost dimensions so low-frequency expensive queries remain visible.
WITH workload AS MATERIALIZED (
    SELECT queryid, calls, total_exec_time, mean_exec_time, rows,
           shared_blks_hit, shared_blks_read, temp_blks_read, temp_blks_written,
           shared_blk_read_time, shared_blk_write_time, wal_bytes
    FROM pg_stat_statements
    WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND queryid IS NOT NULL
),
ranked AS (
    SELECT *,
           row_number() OVER (ORDER BY total_exec_time DESC, queryid) AS total_rank,
           row_number() OVER (ORDER BY calls DESC, queryid) AS calls_rank,
           row_number() OVER (ORDER BY mean_exec_time DESC, queryid) AS mean_rank,
           row_number() OVER (ORDER BY (shared_blks_read::numeric + temp_blks_read + temp_blks_written) DESC, queryid) AS io_rank
    FROM workload
)
SELECT *
FROM ranked
WHERE least(total_rank, calls_rank, mean_rank, io_rank) <= 10
ORDER BY total_rank;
