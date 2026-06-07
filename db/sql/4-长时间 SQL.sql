select
    pid,
    usename,
    datname,
    state,
    now() - query_start as duration,
    query
from pg_stat_activity
where state <> 'idle'
  and query_start is not null
order by duration desc
limit 20;