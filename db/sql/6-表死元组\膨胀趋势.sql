select
    schemaname,
    relname,
    n_live_tup,
    n_dead_tup,
    round(n_dead_tup * 100.0 / nullif(n_live_tup + n_dead_tup, 0), 2) as dead_ratio,
    last_vacuum,
    last_autovacuum,
    last_analyze,
    last_autoanalyze
from pg_stat_user_tables
order by n_dead_tup desc
limit 30;