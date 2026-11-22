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
  stripe_customer_id TEXT, active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS api_keys (
  key_hash TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), prefix TEXT NOT NULL,
  created_at REAL NOT NULL, revoked_at REAL);
CREATE TABLE IF NOT EXISTS usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, event TEXT NOT NULL,
  quantity INTEGER NOT NULL, ts REAL NOT NULL, reported INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS usage_tenant_ts ON usage(tenant_id, ts);
"""


def db_path() -> str:
    return os.environ["PRODUCT_DB"]


@contextmanager
def conn():
    c = sqlite3.connect(db_path(), timeout=10)
    c.row_factory = sqlite3.Row
    try:
        c.executescript(SCHEMA)
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


def update_tenant(tid: str, **fields) -> dict | None:
    allowed = {k: v for k, v in fields.items() if k in ("plan", "active", "stripe_customer_id") and v is not None}
    if "plan" in allowed:
        from product.plans import get_plan
        get_plan(allowed["plan"])
    if allowed:
        with conn() as c:
            c.execute(f"UPDATE tenants SET {', '.join(k + '=?' for k in allowed)} WHERE id=?", (*allowed.values(), tid))
    return get_tenant(tid)


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
