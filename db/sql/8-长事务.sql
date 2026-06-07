select
    pid,
    usename,
    datname,
    state,
    now() - xact_start as xact_duration,
    query
from pg_stat_activity
where xact_start is not null
order by xact_duration desc
limit 20;