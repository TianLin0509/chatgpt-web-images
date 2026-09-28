"""Durable per-login cooling after observed provider HTTP 429 responses."""
import json
import time

BACKOFF=(300,900,1800)


def ensure_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS login_cooldowns (login_group TEXT PRIMARY KEY,retry_after REAL NOT NULL,strikes INTEGER NOT NULL,probe_owner TEXT,probe_until REAL NOT NULL DEFAULT 0)')


def group_of(pool,account_id):
    account=pool.account(account_id)
    return account.get('login_group') or account_id


def get(pool,account_id):
    with pool.connect() as db:
        row=db.execute('SELECT * FROM login_cooldowns WHERE login_group=?',(group_of(pool,account_id),)).fetchone()
    return dict(row) if row else None


def defer(pool,account_id):
    group=group_of(pool,account_id);now=time.time()
    with pool.transaction() as db:
        row=db.execute('SELECT * FROM login_cooldowns WHERE login_group=?',(group,)).fetchone()
        # Concurrent lanes reporting the same burst share one cooling period.
        strikes=(row['strikes'] if row else 0)+(0 if row and row['retry_after']>now else 1)
        retry=row['retry_after'] if row and row['retry_after']>now else now+BACKOFF[min(strikes-1,len(BACKOFF)-1)]
        error={'code':'rate_limited','message':'ChatGPT returned HTTP 429. All lanes of this login are cooling; existing prompts are retained and will not be resent.','retry_at':retry}
        db.execute('INSERT INTO login_cooldowns(login_group,retry_after,strikes) VALUES(?,?,?) ON CONFLICT(login_group) DO UPDATE SET retry_after=excluded.retry_after,strikes=excluded.strikes,probe_owner=NULL,probe_until=0',(group,retry,strikes))
        db.execute("UPDATE accounts SET state='rate_limited',ready=0,error=?,updated=? WHERE COALESCE(NULLIF(login_group,''),id)=?",(json.dumps(error),now,group))
    return error


def acquire_probe(pool,account_id):
    group=group_of(pool,account_id);now=time.time()
    with pool.transaction() as db:
        row=db.execute('SELECT * FROM login_cooldowns WHERE login_group=?',(group,)).fetchone()
        if not row:return True
        if row['retry_after']>now:return False
        if row['probe_until']>now and row['probe_owner']!=account_id:return False
        if row['probe_until']<=now:
            # Resume foreground work before another lane's background parked-job
            # retry. A stalled/dead worker cannot reserve this priority forever.
            foreground=db.execute("SELECT j.account_id FROM jobs j JOIN accounts a ON a.id=j.account_id WHERE COALESCE(NULLIF(a.login_group,''),a.id)=? AND a.enabled=1 AND a.heartbeat>? AND j.status IN ('running','dispatching') ORDER BY CASE j.status WHEN 'running' THEN 0 ELSE 1 END,j.created,j.id LIMIT 1",(group,now-20)).fetchone()
            if foreground and foreground['account_id']!=account_id:return False
        db.execute('UPDATE login_cooldowns SET probe_owner=?,probe_until=? WHERE login_group=?',(account_id,now+180,group))
        return True


def clear(pool,account_id):
    group=group_of(pool,account_id)
    with pool.transaction() as db:
        db.execute('DELETE FROM login_cooldowns WHERE login_group=?',(group,))
        # A successful probe confirms this lane; the other lanes still need their
        # own real operation before they can claim usability.
        db.execute("UPDATE accounts SET state='not_checked',ready=0,error=NULL WHERE state='rate_limited' AND COALESCE(NULLIF(login_group,''),id)=?",(group,))
