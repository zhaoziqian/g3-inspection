select
    datname,
    usename,
    state,
    count(*) as conn_count
from pg_stat_activity
group by datname, usename, state
order by conn_count desc;