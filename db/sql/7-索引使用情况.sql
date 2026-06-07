select
    schemaname,
    relname as table_name,
    indexrelname as index_name,
    idx_scan,
    pg_size_pretty(pg_relation_size(indexrelid)) as index_size
from pg_stat_user_indexes
order by idx_scan asc, pg_relation_size(indexrelid) desc
limit 50;