"""SQLite store: tenants, hashed API keys, usage events. Stdlib only, one connection per call.
API keys are random 256-bit tokens, so a plain SHA-256 digest is sufficient (no password stretching)."""
import hashlib
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, plan TEXT NOT NULL DEFAULT 'free',
  stripe_customer_id TEXT, active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
  stripe_subscription_id TEXT, subscription_status TEXT);
CREATE TABLE IF NOT EXISTS stripe_events (id TEXT PRIMARY KEY, type TEXT NOT NULL, received_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS api_keys (
  key_hash TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), prefix TEXT NOT NULL,
  created_at REAL NOT NULL, revoked_at REAL);
CREATE TABLE IF NOT EXISTS usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, event TEXT NOT NULL,
  quantity INTEGER NOT NULL, ts REAL NOT NULL, reported INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS usage_tenant_ts ON usage(tenant_id, ts);
CREATE TABLE IF NOT EXISTS rate_hits (tenant_id TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS rate_hits_tenant_ts ON rate_hits(tenant_id, ts);
CREATE TABLE IF NOT EXISTS slack_links (team_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id));
CREATE TABLE IF NOT EXISTS signup_hits (ip TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS signup_hits_ts ON signup_hits(ts);
"""


def db_path() -> str:
    return os.environ["PRODUCT_DB"]


def _migrate(c) -> None:
    """Databases created before the Stripe lifecycle lack these tenant columns."""
    have = {r[1] for r in c.execute("PRAGMA table_info(tenants)")}
    for col in ("stripe_subscription_id", "subscription_status"):
        if col not in have:
            try:
                c.execute(f"ALTER TABLE tenants ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:  # another process added it first
                pass


@contextmanager
def conn(immediate: bool = False):
    """immediate=True takes the write lock up front, so read-then-write sequences are atomic across processes."""
    c = sqlite3.connect(db_path(), timeout=10)
    c.row_factory = sqlite3.Row
    try:
        c.executescript(SCHEMA)
        _migrate(c)
        if immediate:
            c.execute("BEGIN IMMEDIATE")
        yield c
        c.commit()
    finally:
        c.close()


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def month_start(now: float | None = None) -> float:
    d = datetime.fromtimestamp(now or time.time(), timezone.utc)
    return datetime(d.year, d.month, 1, tzinfo=timezone.utc).timestamp()


def create_tenant(name: str, plan: str = "free") -> dict:
    from product.plans import get_plan
    get_plan(plan)  # validates
    tid = "ten_" + secrets.token_hex(8)
    with conn() as c:
        c.execute("INSERT INTO tenants(id,name,plan,created_at) VALUES(?,?,?,?)", (tid, name, plan, time.time()))
    return {"id": tid, "name": name, "plan": plan}


def get_tenant(tid: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM tenants WHERE id=?", (tid,)).fetchone()
    return dict(r) if r else None


TENANT_FIELDS = ("plan", "active", "stripe_customer_id", "stripe_subscription_id", "subscription_status")


def update_tenant(tid: str, **fields) -> dict | None:
    allowed = {k: v for k, v in fields.items() if k in TENANT_FIELDS and v is not None}
    if "plan" in allowed:
        from product.plans import get_plan
        get_plan(allowed["plan"])
    if allowed:
        with conn() as c:
            c.execute(f"UPDATE tenants SET {', '.join(k + '=?' for k in allowed)} WHERE id=?", (*allowed.values(), tid))
    return get_tenant(tid)


def tenant_for_customer(customer_id: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM tenants WHERE stripe_customer_id=?", (customer_id,)).fetchone()
    return dict(r) if r else None


def clear_subscription(tid: str, status: str) -> None:
    """update_tenant ignores None, so dropping the subscription id needs its own statement."""
    with conn() as c:
        c.execute("UPDATE tenants SET stripe_subscription_id=NULL, subscription_status=? WHERE id=?", (status, tid))


def issue_key(tid: str) -> str:
    """Returns the plaintext key once; only its hash is stored."""
    key = "adk_" + secrets.token_urlsafe(32)
    with conn() as c:
        c.execute("INSERT INTO api_keys(key_hash,tenant_id,prefix,created_at) VALUES(?,?,?,?)",
                  (hash_key(key), tid, key[:8], time.time()))
    return key


def revoke_key(prefix_or_key: str, tid: str) -> int:
    with conn() as c:
        cur = c.execute("UPDATE api_keys SET revoked_at=? WHERE tenant_id=? AND revoked_at IS NULL AND "
                        "(key_hash=? OR prefix=?)", (time.time(), tid, hash_key(prefix_or_key), prefix_or_key))
        return cur.rowcount


def tenant_for_key(key: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT t.* FROM api_keys k JOIN tenants t ON t.id=k.tenant_id "
                      "WHERE k.key_hash=? AND k.revoked_at IS NULL AND t.active=1", (hash_key(key),)).fetchone()
    return dict(r) if r else None


def record(tid: str, event: str, quantity: int = 1) -> None:
    if quantity > 0:
        with conn() as c:
            c.execute("INSERT INTO usage(tenant_id,event,quantity,ts) VALUES(?,?,?,?)", (tid, event, quantity, time.time()))


def usage_since(tid: str, since: float) -> dict:
    with conn() as c:
        rows = c.execute("SELECT event, SUM(quantity) q FROM usage WHERE tenant_id=? AND ts>=? GROUP BY event",
                         (tid, since)).fetchall()
    return {r["event"]: r["q"] for r in rows}


def rate_hit(tid: str, limit: int, window: float = 60.0) -> bool:
    """Sliding-window limiter shared by every worker/process on this DB. Records the hit and returns True
    if under `limit` requests in the last `window` seconds; a refused request is not recorded."""
    now = time.time()
    with conn(immediate=True) as c:
        c.execute("DELETE FROM rate_hits WHERE tenant_id=? AND ts<=?", (tid, now - window))
        if c.execute("SELECT COUNT(*) FROM rate_hits WHERE tenant_id=?", (tid,)).fetchone()[0] >= limit:
            return False
        c.execute("INSERT INTO rate_hits(tenant_id,ts) VALUES(?,?)", (tid, now))
        return True


def signup_hit(ip: str, per_ip_hour: int, per_day: int) -> bool:
    """Anti-abuse for public signup. Records the attempt and returns True if this IP is under its hourly
    limit and the whole deployment is under its daily limit; a refused attempt is not recorded."""
    now = time.time()
    with conn(immediate=True) as c:
        c.execute("DELETE FROM signup_hits WHERE ts<=?", (now - 86400,))
        if c.execute("SELECT COUNT(*) FROM signup_hits").fetchone()[0] >= per_day:
            return False
        if c.execute("SELECT COUNT(*) FROM signup_hits WHERE ip=? AND ts>?", (ip, now - 3600)).fetchone()[0] >= per_ip_hour:
            return False
        c.execute("INSERT INTO signup_hits(ip,ts) VALUES(?,?)", (ip, now))
        return True


def link_slack(tid: str, team_id: str) -> None:
    """Map a Slack workspace (the slash command's team_id) to a tenant; re-linking moves it."""
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO slack_links(team_id,tenant_id) VALUES(?,?)", (team_id, tid))


def unlink_slack(tid: str, team_id: str) -> int:
    with conn() as c:
        return c.execute("DELETE FROM slack_links WHERE team_id=? AND tenant_id=?", (team_id, tid)).rowcount


def tenant_for_slack(team_id: str | None) -> dict | None:
    if not team_id:
        return None
    with conn() as c:
        r = c.execute("SELECT t.* FROM slack_links l JOIN tenants t ON t.id=l.tenant_id "
                      "WHERE l.team_id=? AND t.active=1", (team_id,)).fetchone()
    return dict(r) if r else None
