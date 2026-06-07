select
    blocked.pid as blocked_pid,
    blocked.usename as blocked_user,
    now() - blocked.query_start as blocked_duration,
    blocked.query as blocked_query,
    blocking.pid as blocking_pid,
    blocking.usename as blocking_user,
    now() - blocking.query_start as blocking_duration,
    blocking.query as blocking_query
from pg_locks blocked_locks
join pg_stat_activity blocked
    on blocked.pid = blocked_locks.pid
join pg_locks blocking_locks
    on blocking_locks.locktype = blocked_locks.locktype
   and blocking_locks.database is not distinct from blocked_locks.database
   and blocking_locks.relation is not distinct from blocked_locks.relation
   and blocking_locks.page is not distinct from blocked_locks.page
   and blocking_locks.tuple is not distinct from blocked_locks.tuple
   and blocking_locks.virtualxid is not distinct from blocked_locks.virtualxid
   and blocking_locks.transactionid is not distinct from blocked_locks.transactionid
   and blocking_locks.classid is not distinct from blocked_locks.classid
   and blocking_locks.objid is not distinct from blocked_locks.objid
   and blocking_locks.objsubid is not distinct from blocked_locks.objsubid
   and blocking_locks.pid <> blocked_locks.pid
join pg_stat_activity blocking
    on blocking.pid = blocking_locks.pid
where not blocked_locks.granted
  and blocking_locks.granted;