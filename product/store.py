"""Store: tenants, hashed API keys, usage events, rate limits.

Two backends, chosen by PRODUCT_DB:
  /data/product.db                     SQLite (stdlib only, one connection per call). One node: fine for
                                       validation and a single replica, not for scaling out.
  postgresql://user:pw@host:5432/db    PostgreSQL (needs `pip install -r product/requirements.txt`). Safe to share
                                       between replicas: quotas, rate limits, signup limits and the billing
                                       schedule all live in the database.
API keys are random 256-bit tokens, so a plain SHA-256 digest is sufficient (no password stretching)."""
import hashlib
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, plan TEXT NOT NULL DEFAULT 'free',
  stripe_customer_id TEXT, active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
  stripe_subscription_id TEXT, subscription_status TEXT, email TEXT,
  terms_version TEXT, terms_accepted_at REAL);
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
CREATE TABLE IF NOT EXISTS recover_hits (ip TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS recover_hits_ts ON recover_hits(ts);
CREATE TABLE IF NOT EXISTS recovery_tokens (
  token_hash TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
  created_at REAL NOT NULL, expires_at REAL NOT NULL, used_at REAL);
CREATE INDEX IF NOT EXISTS recovery_tokens_tenant ON recovery_tokens(tenant_id, created_at);
CREATE TABLE IF NOT EXISTS job_runs (name TEXT PRIMARY KEY, last_run REAL NOT NULL);
"""
# PostgreSQL flavour of the same schema: 64-bit ids and counters, double-precision epoch seconds.
PG_SCHEMA = (re.sub(r"\bREAL\b", "DOUBLE PRECISION",
                    SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
                    .replace("quantity INTEGER", "quantity BIGINT"))
             + "CREATE UNIQUE INDEX IF NOT EXISTS tenants_email_uq ON tenants(email);\n"
             "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS terms_version TEXT;\n"
             "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS terms_accepted_at DOUBLE PRECISION;\n")

RECOVERY_TTL = 1800  # seconds a recovery link stays valid
RECOVERY_COOLDOWN = 600  # at most one recovery email per tenant in this window (stops mailbombing a registered address)


class EmailTaken(Exception):
    pass


def clean_email(value) -> str:
    """Normalised (trimmed, lower-case) address, or ValueError. Deliberately loose: the only real check is
    that a recovery email reaches it. No whitespace is allowed, which also rules out header injection."""
    if not isinstance(value, str):
        raise ValueError("'email' is required")
    email = value.strip().lower()
    if len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("'email' must be a valid email address")
    return email


try:
    import psycopg
    INTEGRITY_ERRORS: tuple = (sqlite3.IntegrityError, psycopg.IntegrityError)
except ImportError:  # SQLite-only install
    psycopg = None
    INTEGRITY_ERRORS = (sqlite3.IntegrityError,)


def db_path() -> str:
    return os.environ["PRODUCT_DB"]


def is_postgres(url: str | None = None) -> bool:
    return (url or db_path()).startswith(("postgres://", "postgresql://"))


def month_sql(col: str) -> str:
    """'YYYY-MM' (UTC) of an epoch-seconds column, in the active dialect."""
    if is_postgres():
        return f"to_char(to_timestamp({col}) AT TIME ZONE 'UTC', 'YYYY-MM')"
    return f"strftime('%Y-%m', {col}, 'unixepoch')"


def _migrate(c) -> None:
    """Databases created before the Stripe lifecycle / key recovery lack these tenant columns (SQLite only;
    PostgreSQL databases are created from the current schema)."""
    have = {r[1] for r in c.execute("PRAGMA table_info(tenants)")}
    for col, typ in (("stripe_subscription_id", "TEXT"), ("subscription_status", "TEXT"), ("email", "TEXT"),
                     ("terms_version", "TEXT"), ("terms_accepted_at", "REAL")):
        if col not in have:
            try:
                c.execute(f"ALTER TABLE tenants ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:  # another process added it first
                pass
    # Emails are stored normalised, so a plain unique index is case-insensitive; NULLs (older tenants) never clash.
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS tenants_email_uq ON tenants(email)")


class _Row:
    """Row that reads like sqlite3.Row: r["name"], r[0], dict(r)."""
    __slots__ = ("_names", "_vals")

    def __init__(self, names, vals):
        self._names, self._vals = names, vals

    def __getitem__(self, k):
        return self._vals[k if isinstance(k, int) else self._names.index(k)]

    def __iter__(self):
        return iter(self._vals)

    def keys(self):
        return list(self._names)


def _row_factory(cursor):
    names = [d.name for d in (cursor.description or ())]
    return lambda vals: _Row(names, vals)


class _PgConn:
    """The slice of the sqlite3 connection API the store uses, over a pooled psycopg connection."""

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql: str, params=()):
        return self._raw.execute(sql.replace("%", "%%").replace("?", "%s"), params)


_pools: dict = {}
_pools_lock = threading.Lock()


def _pg_pool(url: str):
    """One pool per process (created lazily, so it is safe with forked workers); the schema is applied once."""
    with _pools_lock:
        if url not in _pools:
            if psycopg is None:
                raise RuntimeError("PostgreSQL needs psycopg: pip install -r product/requirements.txt")
            from psycopg_pool import ConnectionPool
            pool = ConnectionPool(url, min_size=1, max_size=int(os.getenv("PRODUCT_DB_POOL_MAX", "10")),
                                  kwargs={"row_factory": _row_factory}, open=False)
            pool.open(wait=True, timeout=30)
            with pool.connection() as raw:  # serialise concurrent first starts of several replicas
                raw.execute("SELECT pg_advisory_xact_lock(hashtext('product-schema'))")
                raw.execute(PG_SCHEMA)
            _pools[url] = pool
        return _pools[url]


def close_pools() -> None:
    """Close every PostgreSQL pool (shutdown, or tests that switch databases)."""
    with _pools_lock:
        for pool in _pools.values():
            pool.close()
        _pools.clear()


@contextmanager
def conn(immediate: bool = False, lock: str | None = None):
    """immediate=True makes read-then-write sequences atomic across processes. SQLite takes the database write
    lock up front; PostgreSQL takes a transaction-scoped advisory lock on `lock` (default: one global lock), so
    callers that are independent per tenant pass the tenant id and do not queue behind each other."""
    url = db_path()
    if is_postgres(url):
        with _pg_pool(url).connection() as raw:  # commits on success, rolls back on error
            if immediate:
                raw.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock or "product-db",))
            yield _PgConn(raw)
        return
    c = sqlite3.connect(url, timeout=10)
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


def create_tenant(name: str, plan: str = "free", email: str | None = None, terms_version: str | None = None) -> dict:
    """`email` must already be normalised (clean_email). Raises EmailTaken if another tenant has it.
    `terms_version` records that the signer accepted that version of the legal documents (and when)."""
    from product.plans import get_plan
    get_plan(plan)  # validates
    tid = "ten_" + secrets.token_hex(8)
    try:
        with conn() as c:
            now = time.time()
            c.execute("INSERT INTO tenants(id,name,plan,created_at,email,terms_version,terms_accepted_at) "
                      "VALUES(?,?,?,?,?,?,?)",
                      (tid, name, plan, now, email, terms_version, now if terms_version else None))
    except INTEGRITY_ERRORS:  # the only unique column besides the random id is email
        raise EmailTaken(email)
    return {"id": tid, "name": name, "plan": plan, **({"email": email} if email else {})}


def get_tenant(tid: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM tenants WHERE id=?", (tid,)).fetchone()
    return dict(r) if r else None


def tenant_for_email(email: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM tenants WHERE email=?", (email,)).fetchone()
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


def _insert_key(c, tid: str) -> str:
    key = "adk_" + secrets.token_urlsafe(32)
    c.execute("INSERT INTO api_keys(key_hash,tenant_id,prefix,created_at) VALUES(?,?,?,?)",
              (hash_key(key), tid, key[:8], time.time()))
    return key


def issue_key(tid: str) -> str:
    """Returns the plaintext key once; only its hash is stored."""
    with conn() as c:
        return _insert_key(c, tid)


def start_recovery(tid: str) -> str | None:
    """Single-use recovery token for the tenant (plaintext returned once, only its hash is stored), or None if
    one was already issued within RECOVERY_COOLDOWN. A new token replaces the tenant's older ones."""
    now = time.time()
    token = secrets.token_urlsafe(32)
    with conn(immediate=True, lock=f"recovery:{tid}") as c:
        if c.execute("SELECT 1 FROM recovery_tokens WHERE tenant_id=? AND created_at>?",
                     (tid, now - RECOVERY_COOLDOWN)).fetchone():
            return None
        c.execute("DELETE FROM recovery_tokens WHERE tenant_id=? OR expires_at<=?", (tid, now))
        c.execute("INSERT INTO recovery_tokens(token_hash,tenant_id,created_at,expires_at) VALUES(?,?,?,?)",
                  (hash_key(token), tid, now, now + RECOVERY_TTL))
    return token


def redeem_recovery(token: str) -> tuple[dict, str] | None:
    """Spend a recovery token: revoke every key the tenant has and issue one new key, atomically.
    Returns (tenant, plaintext key), or None if the token is unknown, used, expired or the tenant is inactive."""
    now = time.time()
    with conn(immediate=True, lock="recovery-redeem:" + hash_key(token)) as c:
        r = c.execute("SELECT r.tenant_id FROM recovery_tokens r JOIN tenants t ON t.id=r.tenant_id "
                      "WHERE r.token_hash=? AND r.used_at IS NULL AND r.expires_at>? AND t.active=1",
                      (hash_key(token), now)).fetchone()
        if not r:
            return None
        tid = r["tenant_id"]
        c.execute("UPDATE recovery_tokens SET used_at=? WHERE token_hash=?", (now, hash_key(token)))
        c.execute("UPDATE api_keys SET revoked_at=? WHERE tenant_id=? AND revoked_at IS NULL", (now, tid))
        key = _insert_key(c, tid)
        tenant = dict(c.execute("SELECT * FROM tenants WHERE id=?", (tid,)).fetchone())
    return tenant, key


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


class QuotaRefused(Exception):
    pass


def record(tid: str, event: str, quantity: int = 1) -> None:
    if quantity > 0:
        with conn() as c:
            c.execute("INSERT INTO usage(tenant_id,event,quantity,ts) VALUES(?,?,?,?)", (tid, event, quantity, time.time()))


def reserve(tid: str, event: str, quantity: int, check) -> int:
    """Atomic check-and-record: under the write lock, `check(used_this_month)` returns a refusal reason or None;
    if None the usage row is inserted in the same transaction and its id returned (pass it to `release` if the
    work fails). Raises QuotaRefused(reason) otherwise. Concurrent requests can no longer overshoot a limit."""
    with conn(immediate=True, lock=f"usage:{tid}") as c:
        now = time.time()
        rows = c.execute("SELECT event, CAST(SUM(quantity) AS BIGINT) q FROM usage WHERE tenant_id=? AND ts>=? "
                         "GROUP BY event", (tid, month_start(now))).fetchall()
        reason = check({r["event"]: r["q"] for r in rows})
        if reason:
            raise QuotaRefused(reason)
        return c.execute("INSERT INTO usage(tenant_id,event,quantity,ts) VALUES(?,?,?,?) RETURNING id",
                         (tid, event, quantity, now)).fetchall()[0][0]


def release(row_id: int) -> None:
    """Undo a reservation whose work failed. Rows already reported to Stripe are left alone."""
    with conn() as c:
        c.execute("DELETE FROM usage WHERE id=? AND reported=0", (row_id,))


def usage_since(tid: str, since: float) -> dict:
    with conn() as c:
        rows = c.execute("SELECT event, CAST(SUM(quantity) AS BIGINT) q FROM usage WHERE tenant_id=? AND ts>=? "
                         "GROUP BY event", (tid, since)).fetchall()
    return {r["event"]: r["q"] for r in rows}


def rate_hit(tid: str, limit: int, window: float = 60.0) -> bool:
    """Sliding-window limiter shared by every worker/process on this DB. Records the hit and returns True
    if under `limit` requests in the last `window` seconds; a refused request is not recorded."""
    now = time.time()
    with conn(immediate=True, lock=f"rate:{tid}") as c:
        c.execute("DELETE FROM rate_hits WHERE tenant_id=? AND ts<=?", (tid, now - window))
        if c.execute("SELECT COUNT(*) FROM rate_hits WHERE tenant_id=?", (tid,)).fetchone()[0] >= limit:
            return False
        c.execute("INSERT INTO rate_hits(tenant_id,ts) VALUES(?,?)", (tid, now))
        return True


def _hit(table: str, ip: str, per_ip_hour: int, per_day: int) -> bool:
    now = time.time()
    with conn(immediate=True, lock=table) as c:  # `table` is always one of our own constants, never user input
        c.execute(f"DELETE FROM {table} WHERE ts<=?", (now - 86400,))
        if c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] >= per_day:
            return False
        if c.execute(f"SELECT COUNT(*) FROM {table} WHERE ip=? AND ts>?", (ip, now - 3600)).fetchone()[0] >= per_ip_hour:
            return False
        c.execute(f"INSERT INTO {table}(ip,ts) VALUES(?,?)", (ip, now))
        return True


def signup_hit(ip: str, per_ip_hour: int, per_day: int) -> bool:
    """Anti-abuse for public signup. Records the attempt and returns True if this IP is under its hourly
    limit and the whole deployment is under its daily limit; a refused attempt is not recorded."""
    return _hit("signup_hits", ip, per_ip_hour, per_day)


def recover_hit(ip: str, per_ip_hour: int, per_day: int) -> bool:
    """Same limiter for key-recovery requests, with its own counters so signups and recoveries don't share a budget."""
    return _hit("recover_hits", ip, per_ip_hour, per_day)


def link_slack(tid: str, team_id: str) -> None:
    """Map a Slack workspace (the slash command's team_id) to a tenant; re-linking moves it."""
    with conn() as c:
        c.execute("INSERT INTO slack_links(team_id,tenant_id) VALUES(?,?) "
                  "ON CONFLICT(team_id) DO UPDATE SET tenant_id=excluded.tenant_id", (team_id, tid))


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


def claim_run(name: str, interval: float) -> bool:
    """True if this caller should run the periodic job `name` now: nobody has run it in the last `interval`
    seconds. The claim is atomic, so with several replicas (or workers) sharing this database exactly one of
    them runs the job per interval."""
    now = time.time()
    with conn(immediate=True, lock=f"job:{name}") as c:
        r = c.execute("SELECT last_run FROM job_runs WHERE name=?", (name,)).fetchone()
        if r and now - r["last_run"] < interval:
            return False
        c.execute("INSERT INTO job_runs(name,last_run) VALUES(?,?) "
                  "ON CONFLICT(name) DO UPDATE SET last_run=excluded.last_run", (name, now))
        return True
