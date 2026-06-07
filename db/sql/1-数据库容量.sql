select
    datname as database_name,
    pg_size_pretty(pg_database_size(datname)) as size,
    pg_database_size(datname) as size_bytes
from pg_database
where datname not in ('template0', 'template1','templatem')
order by pg_database_size(datname) desc;