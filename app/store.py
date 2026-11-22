import sqlite3,json,os
from datetime import datetime,timezone
from pathlib import Path
from app.errors import DomainError

def connection():
    path=Path(os.environ.get('DB_PATH','data/records.db'));path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=10);db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, body TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, decision TEXT NOT NULL DEFAULT "待复核")')
    db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS settlements (run_id TEXT PRIMARY KEY, period TEXT NOT NULL, status TEXT NOT NULL, paid INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, superseded_by TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS payments (run_id TEXT PRIMARY KEY, period TEXT NOT NULL, amount TEXT NOT NULL, paid_at TEXT NOT NULL)')
    return db

def save(run_id, body):
    with connection() as db:
        db.execute('INSERT OR IGNORE INTO runs(id,body) VALUES (?,?)',(run_id,json.dumps(body,ensure_ascii=False)))
        db.execute("INSERT INTO meta(key,value) VALUES ('latest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(run_id,))
        monthly=body.get('monthly')
        if monthly:
            now=datetime.now(timezone.utc).isoformat()
            period=monthly['period']
            existing=db.execute('SELECT 1 FROM settlements WHERE run_id=?',(run_id,)).fetchone()
            if not existing:
                # 同一期间的旧有效月结先作废、再落新单；任一步失败整事务回滚，旧单保持有效。
                # 只处理真正带月结的单，打卡阶段批次（无 monthly）不占用期间槽位。
                db.execute("UPDATE settlements SET status='作废', superseded_by=? WHERE period=? AND status='有效'",
                           (run_id,period))
                # 同源重跑复用 run_id：INSERT 保证不重复建单、不重复付款。
                db.execute('INSERT INTO settlements(run_id,period,status,paid,created_at) VALUES (?,?,?,?,?)',
                           (run_id,period,'有效' if monthly.get('balanced') else '待核查',0,now))
    return get(run_id)

def get(run_id):
    with connection() as db:r=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not r:raise DomainError('NOT_FOUND','批次不存在',404)
    body=json.loads(r['body']);body.update(run_id=r['id'],review_version=r['version'],decision=r['decision'])
    with connection() as db:
        s=db.execute('SELECT status,paid FROM settlements WHERE run_id=?',(run_id,)).fetchone()
    if s:
        body['settlement']={'status':s['status'],'paid':bool(s['paid'])}
    return body

def history():
    with connection() as db:rows=db.execute("SELECT id,version,decision FROM runs ORDER BY CASE WHEN id=(SELECT value FROM meta WHERE key='latest') THEN 0 ELSE 1 END,rowid DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]

def review(run_id, version, decision):
    if decision not in ('通过','退回'):raise DomainError('DECISION','复核状态无效')
    with connection() as db:
        cur=db.execute('UPDATE runs SET version=version+1,decision=? WHERE id=? AND version=?',(decision,run_id,version))
        if cur.rowcount!=1:
            if not db.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():raise DomainError('NOT_FOUND','批次不存在',404)
            raise DomainError('STALE','复核版本已更新，请刷新',409)
    return get(run_id)

def list_settlements():
    """按期间返回月结单状态；页面与导出只认 status=有效 的单。"""
    with connection() as db:
        rows=db.execute('SELECT run_id,period,status,paid,superseded_by,created_at FROM settlements ORDER BY period,rowid').fetchall()
    return [dict(r) for r in rows]

def _settlement(db, run_id):
    s=db.execute('SELECT * FROM settlements WHERE run_id=?',(run_id,)).fetchone()
    if not s:raise DomainError('NOT_SETTLEMENT','该批次没有付款月结单',404)
    return s

def mark_paid(run_id):
    """付款登记：幂等。重跑同一批返回既有付款记录，绝不重复付款。"""
    with connection() as db:
        s=_settlement(db,run_id)
        if s['status']!='有效':raise DomainError('VOID','月结单已作废，不能付款',409)
        if s['paid']:
            row=db.execute('SELECT * FROM payments WHERE run_id=?',(run_id,)).fetchone()
            return dict(row)
        body=json.loads(db.execute('SELECT body FROM runs WHERE id=?',(run_id,)).fetchone()['body'])
        amount=body['monthly']['totals']['payable']
        now=datetime.now(timezone.utc).isoformat()
        db.execute('INSERT INTO payments(run_id,period,amount,paid_at) VALUES (?,?,?,?)',
                   (run_id,s['period'],amount,now))
        db.execute('UPDATE settlements SET paid=1 WHERE run_id=?',(run_id,))
        row=db.execute('SELECT * FROM payments WHERE run_id=?',(run_id,)).fetchone()
    return dict(row)

def payment(run_id):
    with connection() as db:
        _settlement(db,run_id)
        row=db.execute('SELECT * FROM payments WHERE run_id=?',(run_id,)).fetchone()
    return dict(row) if row else None
