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
  terms_version TEXT, terms_accepted_at REAL, email_verified_at REAL, seats_synced INTEGER);
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
CREATE TABLE IF NOT EXISTS verify_tokens (
  token_hash TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
  created_at REAL NOT NULL, expires_at REAL NOT NULL, used_at REAL);
CREATE INDEX IF NOT EXISTS verify_tokens_tenant ON verify_tokens(tenant_id, created_at);
CREATE TABLE IF NOT EXISTS job_runs (name TEXT PRIMARY KEY, last_run REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tenant_members (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), email TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'member', status TEXT NOT NULL DEFAULT 'invited', oidc_subject TEXT,
  created_at REAL NOT NULL, accepted_at REAL, removed_at REAL);
CREATE UNIQUE INDEX IF NOT EXISTS tenant_members_email_uq ON tenant_members(tenant_id, email);
CREATE UNIQUE INDEX IF NOT EXISTS tenant_members_sub_uq ON tenant_members(tenant_id, oidc_subject);
CREATE TABLE IF NOT EXISTS member_invites (
  token_hash TEXT PRIMARY KEY, member_id TEXT NOT NULL REFERENCES tenant_members(id),
  tenant_id TEXT NOT NULL REFERENCES tenants(id), created_at REAL NOT NULL, expires_at REAL NOT NULL, used_at REAL);
CREATE INDEX IF NOT EXISTS member_invites_member ON member_invites(member_id);
CREATE TABLE IF NOT EXISTS invite_hits (ip TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS invite_hits_ts ON invite_hits(ts);
CREATE TABLE IF NOT EXISTS sso_hits (ip TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS sso_hits_ts ON sso_hits(ts);
CREATE TABLE IF NOT EXISTS tenant_sso (
  tenant_id TEXT PRIMARY KEY REFERENCES tenants(id), issuer TEXT NOT NULL, client_id TEXT NOT NULL,
  client_secret_enc TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, updated_at REAL NOT NULL);
"""
# PostgreSQL flavour of the same schema: 64-bit ids and counters, double-precision epoch seconds.
PG_SCHEMA = (re.sub(r"\bREAL\b", "DOUBLE PRECISION",
                    SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
                    .replace("quantity INTEGER", "quantity BIGINT"))
             + "CREATE UNIQUE INDEX IF NOT EXISTS tenants_email_uq ON tenants(email);\n"
             "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS terms_version TEXT;\n"
             "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS terms_accepted_at DOUBLE PRECISION;\n"
             "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS email_verified_at DOUBLE PRECISION;\n"
             "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS seats_synced BIGINT;\n")

RECOVERY_TTL = 1800  # seconds a recovery link stays valid
VERIFY_TTL = 86400  # seconds a verification link stays valid
INVITE_TTL = 7 * 86400  # seconds a member invitation stays valid
ROLES = ("owner", "admin", "member")
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
                     ("terms_version", "TEXT"), ("terms_accepted_at", "REAL"), ("email_verified_at", "REAL"),
                     ("seats_synced", "INTEGER")):
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


def _reclaim_email(email: str, after: float) -> bool:
    """Free `email` if it is only squatted: its holder never proved the mailbox within `after` seconds, is on
    the free plan with no subscription, and was sent a verification link (so it signed up while verification was
    on; older accounts are never touched). The holder keeps its keys, it just loses the address."""
    with conn(immediate=True, lock="reclaim:" + email) as c:
        freed = c.execute(
            "UPDATE tenants SET email=NULL WHERE email=? AND email_verified_at IS NULL AND plan='free' "
            "AND stripe_subscription_id IS NULL AND created_at<? "
            "AND EXISTS (SELECT 1 FROM verify_tokens v WHERE v.tenant_id=tenants.id)",
            (email, time.time() - after)).rowcount > 0
        if freed:  # the squatter's owner seat carried that address too: it must not outlive the claim
            c.execute("UPDATE tenant_members SET status='removed', removed_at=? WHERE email=? AND role='owner' "
                      "AND tenant_id IN (SELECT id FROM tenants WHERE email IS NULL)", (time.time(), email))
        return freed


def create_tenant(name: str, plan: str = "free", email: str | None = None, terms_version: str | None = None,
                  reclaim_after: float | None = None) -> dict:
    """`email` must already be normalised (clean_email). Raises EmailTaken if another tenant has it, unless that
    tenant's claim is unverified and older than `reclaim_after` seconds (see _reclaim_email).
    `terms_version` records that the signer accepted that version of the legal documents (and when)."""
    from product.plans import get_plan
    get_plan(plan)  # validates
    tid = "ten_" + secrets.token_hex(8)
    for attempt in (0, 1):
        try:
            with conn() as c:
                now = time.time()
                c.execute("INSERT INTO tenants(id,name,plan,created_at,email,terms_version,terms_accepted_at) "
                          "VALUES(?,?,?,?,?,?,?)",
                          (tid, name, plan, now, email, terms_version, now if terms_version else None))
            break
        except INTEGRITY_ERRORS:  # the only unique column besides the random id is email
            if attempt or not (email and reclaim_after is not None and _reclaim_email(email, reclaim_after)):
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


def set_seats_synced(tid: str, n: int | None) -> None:
    """The seat quantity Stripe is known to bill (None = unknown); update_tenant cannot store None."""
    with conn() as c:
        c.execute("UPDATE tenants SET seats_synced=? WHERE id=?", (n, tid))


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
        c.execute("UPDATE tenants SET email_verified_at=? WHERE id=? AND email_verified_at IS NULL", (now, tid))  # opening the mailed link proves the mailbox
        key = _insert_key(c, tid)
        tenant = dict(c.execute("SELECT * FROM tenants WHERE id=?", (tid,)).fetchone())
    return tenant, key


def start_verification(tid: str) -> str | None:
    """Single-use token proving control of the tenant's mailbox (plaintext returned once, only its hash stored),
    or None if the tenant has no email, is already verified, or one was sent within RECOVERY_COOLDOWN."""
    now = time.time()
    token = secrets.token_urlsafe(32)
    with conn(immediate=True, lock=f"verify:{tid}") as c:
        t = c.execute("SELECT email, email_verified_at FROM tenants WHERE id=?", (tid,)).fetchone()
        if not t or not t["email"] or t["email_verified_at"]:
            return None
        if c.execute("SELECT 1 FROM verify_tokens WHERE tenant_id=? AND created_at>?",
                     (tid, now - RECOVERY_COOLDOWN)).fetchone():
            return None
        c.execute("DELETE FROM verify_tokens WHERE tenant_id=? OR expires_at<=?", (tid, now))
        c.execute("INSERT INTO verify_tokens(token_hash,tenant_id,created_at,expires_at) VALUES(?,?,?,?)",
                  (hash_key(token), tid, now, now + VERIFY_TTL))
    return token


def redeem_verification(token: str) -> dict | None:
    """Spend a verification token and mark the tenant's email verified. None if unknown, used or expired."""
    now = time.time()
    with conn(immediate=True, lock="verify-redeem:" + hash_key(token)) as c:
        r = c.execute("SELECT tenant_id FROM verify_tokens WHERE token_hash=? AND used_at IS NULL AND expires_at>?",
                      (hash_key(token), now)).fetchone()
        if not r:
            return None
        c.execute("UPDATE verify_tokens SET used_at=? WHERE token_hash=?", (now, hash_key(token)))
        c.execute("UPDATE tenants SET email_verified_at=? WHERE id=? AND email_verified_at IS NULL", (now, r["tenant_id"]))
        return dict(c.execute("SELECT * FROM tenants WHERE id=?", (r["tenant_id"],)).fetchone())


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


# ---- tenant members (seats) --------------------------------------------------------------------------
#
# A seat is an active row in tenant_members. Pending invitations hold a place against the plan's max_seats but are
# not billed until accepted. Removal keeps the row (status 'removed') so the audit trail and the SSO subject
# binding survive a re-invite. API keys stay tenant-level: they act as the tenant's owner and are not seats.

class SeatLimit(Exception):
    """The plan's max_seats would be exceeded."""


class MemberError(Exception):
    """A refused member change; the message is safe to show to the caller."""


def _member(r) -> dict:
    d = dict(r)
    d.pop("oidc_subject", None)  # an IdP subject is internal; callers only learn whether the member is bound
    d["sso_linked"] = bool(r["oidc_subject"])
    return d


def ensure_owner(tid: str) -> None:
    """Tenants predate members: give the tenant's signup email an owner seat the first time anyone looks. The
    owner counts as a seat, and is active at once (the tenant's own email was the signup identity)."""
    with conn() as c:  # the common case is "already there": no write lock for that
        if c.execute("SELECT 1 FROM tenant_members WHERE tenant_id=? AND role='owner'", (tid,)).fetchone():
            return
    with conn(immediate=True, lock=f"members:{tid}") as c:
        if c.execute("SELECT 1 FROM tenant_members WHERE tenant_id=? AND role='owner'", (tid,)).fetchone():
            return
        t = c.execute("SELECT email FROM tenants WHERE id=?", (tid,)).fetchone()
        if not t or not t["email"]:
            return
        now = time.time()
        c.execute("INSERT INTO tenant_members(id,tenant_id,email,role,status,created_at,accepted_at) "
                  "VALUES(?,?,?,?,?,?,?) ON CONFLICT(tenant_id,email) DO NOTHING",
                  ("mem_" + secrets.token_hex(8), tid, t["email"], "owner", "active", now, now))


def list_members(tid: str) -> list[dict]:
    ensure_owner(tid)
    with conn() as c:
        rows = c.execute("SELECT * FROM tenant_members WHERE tenant_id=? AND status!='removed' ORDER BY created_at, id",
                         (tid,)).fetchall()
    return [_member(r) for r in rows]


def get_member(tid: str, mid: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM tenant_members WHERE tenant_id=? AND id=?", (tid, mid)).fetchone()
    return _member(r) if r else None


def seat_count(tid: str) -> int:
    """Active members: what Stripe bills. Never below 1, because a tenant with no member rows (operator-created,
    no email) still has its owner using the API keys."""
    ensure_owner(tid)
    with conn() as c:
        n = c.execute("SELECT COUNT(*) FROM tenant_members WHERE tenant_id=? AND status='active'", (tid,)).fetchone()[0]
    return max(1, n)


def active_members(tid: str) -> int:
    """Raw count of active members (seat_count is never below 1)."""
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM tenant_members WHERE tenant_id=? AND status='active'", (tid,)).fetchone()[0]


def _held(c, tid: str, now: float) -> int:
    """Active members plus invitations still open: what max_seats limits."""
    return c.execute(
        "SELECT COUNT(*) FROM tenant_members m WHERE m.tenant_id=? AND (m.status='active' OR (m.status='invited' AND "
        "EXISTS (SELECT 1 FROM member_invites i WHERE i.member_id=m.id AND i.used_at IS NULL AND i.expires_at>?)))",
        (tid, now)).fetchone()[0]


def invite_member(tid: str, email: str, role: str, max_seats: int | None) -> tuple[dict, str]:
    """Create (or re-open) a pending member and a single-use invitation token (plaintext returned once, only its
    hash stored). Raises MemberError (already a member, bad role, cooldown) or SeatLimit."""
    if role not in ROLES or role == "owner":
        raise MemberError("'role' must be 'admin' or 'member'")
    ensure_owner(tid)
    now = time.time()
    token = secrets.token_urlsafe(32)
    with conn(immediate=True, lock=f"members:{tid}") as c:
        m = c.execute("SELECT * FROM tenant_members WHERE tenant_id=? AND email=?", (tid, email)).fetchone()
        if m and m["status"] == "active":
            raise MemberError("that person is already a member")
        if m and c.execute("SELECT 1 FROM member_invites WHERE member_id=? AND created_at>?",
                           (m["id"], now - RECOVERY_COOLDOWN)).fetchone():
            raise MemberError("an invitation was just sent to that address; wait a few minutes before sending another")
        if not (m and m["status"] == "invited" and c.execute(  # re-sending an open invite does not take a second place
                "SELECT 1 FROM member_invites WHERE member_id=? AND used_at IS NULL AND expires_at>?",
                (m["id"], now)).fetchone()):
            if max_seats is not None and _held(c, tid, now) >= max_seats:
                raise SeatLimit(max_seats)
        if m:
            mid = m["id"]
            c.execute("UPDATE tenant_members SET role=?, status='invited', removed_at=NULL WHERE id=?", (role, mid))
        else:
            mid = "mem_" + secrets.token_hex(8)
            c.execute("INSERT INTO tenant_members(id,tenant_id,email,role,status,created_at) VALUES(?,?,?,?,?,?)",
                      (mid, tid, email, role, "invited", now))
        c.execute("DELETE FROM member_invites WHERE member_id=? OR expires_at<=?", (mid, now))
        c.execute("INSERT INTO member_invites(token_hash,member_id,tenant_id,created_at,expires_at) VALUES(?,?,?,?,?)",
                  (hash_key(token), mid, tid, now, now + INVITE_TTL))
        member = _member(c.execute("SELECT * FROM tenant_members WHERE id=?", (mid,)).fetchone())
    return member, token


def _activate(c, m, now: float, max_seats: int | None, subject: str | None = None) -> None:
    """Turn an invited member active inside the caller's transaction. Re-checks the cap, because the plan may
    have changed since the invitation was sent; an invitation's own place is already counted, so it is excluded."""
    if max_seats is not None:
        active = c.execute("SELECT COUNT(*) FROM tenant_members WHERE tenant_id=? AND status='active'",
                           (m["tenant_id"],)).fetchone()[0]
        if active >= max_seats:
            raise SeatLimit(max_seats)
    c.execute("UPDATE tenant_members SET status='active', accepted_at=?, removed_at=NULL, "
              "oidc_subject=COALESCE(oidc_subject, ?) WHERE id=?", (now, subject, m["id"]))
    c.execute("UPDATE member_invites SET used_at=? WHERE member_id=? AND used_at IS NULL", (now, m["id"]))


def redeem_invite(token: str, max_seats_for) -> dict | None:
    """Spend an invitation: the member becomes active (opening the mailed link proves the mailbox). None if the
    token is unknown, used or expired, or the tenant is inactive. `max_seats_for(tenant_row)` gives the cap for the
    tenant's current plan. Raises SeatLimit."""
    now = time.time()
    with conn(immediate=True, lock="invite-redeem:" + hash_key(token)) as c:
        r = c.execute("SELECT i.member_id, i.tenant_id FROM member_invites i JOIN tenants t ON t.id=i.tenant_id "
                      "WHERE i.token_hash=? AND i.used_at IS NULL AND i.expires_at>? AND t.active=1",
                      (hash_key(token), now)).fetchone()
        if not r:
            return None
        m = c.execute("SELECT * FROM tenant_members WHERE id=?", (r["member_id"],)).fetchone()
        if not m or m["status"] == "removed":
            return None
        if m["status"] != "active":
            t = c.execute("SELECT * FROM tenants WHERE id=?", (r["tenant_id"],)).fetchone()
            _activate(c, m, now, max_seats_for(dict(t)))
        else:
            c.execute("UPDATE member_invites SET used_at=? WHERE member_id=? AND used_at IS NULL", (now, m["id"]))
        return _member(c.execute("SELECT * FROM tenant_members WHERE id=?", (m["id"],)).fetchone())


def remove_member(tid: str, mid: str) -> dict | None:
    """Soft-remove: the member loses their seat and every session at once (sessions are re-checked against this
    row). The last owner cannot be removed. Returns the member, or None if there is no such member."""
    now = time.time()
    with conn(immediate=True, lock=f"members:{tid}") as c:
        m = c.execute("SELECT * FROM tenant_members WHERE tenant_id=? AND id=? AND status!='removed'", (tid, mid)).fetchone()
        if not m:
            return None
        if m["role"] == "owner" and m["status"] == "active" and c.execute(
                "SELECT COUNT(*) FROM tenant_members WHERE tenant_id=? AND role='owner' AND status='active'",
                (tid,)).fetchone()[0] <= 1:
            raise MemberError("a tenant needs at least one owner")
        c.execute("UPDATE tenant_members SET status='removed', removed_at=? WHERE id=?", (now, mid))
        c.execute("DELETE FROM member_invites WHERE member_id=?", (mid,))
        return _member(c.execute("SELECT * FROM tenant_members WHERE id=?", (mid,)).fetchone())


def set_member_role(tid: str, mid: str, role: str) -> dict | None:
    if role not in ROLES:
        raise MemberError("'role' must be one of owner, admin, member")
    with conn(immediate=True, lock=f"members:{tid}") as c:
        m = c.execute("SELECT * FROM tenant_members WHERE tenant_id=? AND id=? AND status!='removed'", (tid, mid)).fetchone()
        if not m:
            return None
        if m["role"] == "owner" and role != "owner" and m["status"] == "active" and c.execute(
                "SELECT COUNT(*) FROM tenant_members WHERE tenant_id=? AND role='owner' AND status='active'",
                (tid,)).fetchone()[0] <= 1:
            raise MemberError("a tenant needs at least one owner")
        c.execute("UPDATE tenant_members SET role=? WHERE id=?", (role, mid))
        return _member(c.execute("SELECT * FROM tenant_members WHERE id=?", (mid,)).fetchone())


def sso_member(tid: str, subject: str, email: str | None, max_seats_for) -> dict | None:
    """The member an OIDC login stands for, or None. A member already bound to this IdP subject matches on the
    subject alone (their email at the IdP may change). Otherwise an *invited or active* member with the same
    email and no subject yet is bound to it, and an invited one becomes active: the IdP vouched for the mailbox,
    which is what the emailed link would have proved. Nobody is created here: SSO never adds seats by itself.
    Raises SeatLimit when activating would pass the plan's cap."""
    now = time.time()
    with conn(immediate=True, lock=f"members:{tid}") as c:
        m = c.execute("SELECT * FROM tenant_members WHERE tenant_id=? AND oidc_subject=?", (tid, subject)).fetchone()
        if not m and email:
            m = c.execute("SELECT * FROM tenant_members WHERE tenant_id=? AND email=? AND oidc_subject IS NULL "
                          "AND status IN ('invited','active')", (tid, email)).fetchone()
            if m and m["status"] == "invited" and not c.execute(
                    "SELECT 1 FROM member_invites WHERE member_id=? AND used_at IS NULL AND expires_at>?",
                    (m["id"], now)).fetchone():
                m = None  # an expired invitation is not an invitation
        if not m or m["status"] == "removed":
            return None
        if m["status"] == "invited":
            t = c.execute("SELECT * FROM tenants WHERE id=?", (tid,)).fetchone()
            _activate(c, m, now, max_seats_for(dict(t)), subject)
        elif not m["oidc_subject"]:
            c.execute("UPDATE tenant_members SET oidc_subject=? WHERE id=?", (subject, m["id"]))
        return _member(c.execute("SELECT * FROM tenant_members WHERE id=?", (m["id"],)).fetchone())


def invite_hit(tid: str, per_tenant_hour: int, per_day: int) -> bool:
    """Anti-abuse for invitation email: a tenant cannot make us mail arbitrary addresses without limit."""
    return _hit("invite_hits", tid, per_tenant_hour, per_day)


def sso_hit(ip: str, per_ip_hour: int, per_day: int) -> bool:
    """Limiter for the public SSO login endpoint, which makes this server fetch a tenant-chosen URL."""
    return _hit("sso_hits", ip, per_ip_hour, per_day)


# ---- per-tenant OIDC configuration -----------------------------------------------------------------

def get_sso(tid: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM tenant_sso WHERE tenant_id=?", (tid,)).fetchone()
    return dict(r) if r else None


def put_sso(tid: str, issuer: str, client_id: str, client_secret_enc: str, enabled: bool = True) -> None:
    """Save the tenant's IdP. Subjects are only meaningful for one issuer and client (Entra's `sub` differs per
    application), so changing either unbinds every member: each is matched by email again at their next login."""
    with conn(immediate=True, lock=f"members:{tid}") as c:
        old = c.execute("SELECT issuer, client_id FROM tenant_sso WHERE tenant_id=?", (tid,)).fetchone()
        if old and (old["issuer"], old["client_id"]) != (issuer, client_id):
            c.execute("UPDATE tenant_members SET oidc_subject=NULL WHERE tenant_id=?", (tid,))
        c.execute("INSERT INTO tenant_sso(tenant_id,issuer,client_id,client_secret_enc,enabled,updated_at) "
                  "VALUES(?,?,?,?,?,?) ON CONFLICT(tenant_id) DO UPDATE SET issuer=excluded.issuer, "
                  "client_id=excluded.client_id, client_secret_enc=excluded.client_secret_enc, "
                  "enabled=excluded.enabled, updated_at=excluded.updated_at",
                  (tid, issuer, client_id, client_secret_enc, 1 if enabled else 0, time.time()))


def set_sso_enabled(tid: str, enabled: bool) -> bool:
    with conn() as c:
        return c.execute("UPDATE tenant_sso SET enabled=?, updated_at=? WHERE tenant_id=?",
                         (1 if enabled else 0, time.time(), tid)).rowcount > 0


def delete_sso(tid: str) -> int:
    with conn() as c:
        return c.execute("DELETE FROM tenant_sso WHERE tenant_id=?", (tid,)).rowcount


def live_member(tid: str, mid: str) -> dict | None:
    """The member if still active and the tenant is too: what a session is re-checked against on every request."""
    with conn() as c:
        r = c.execute("SELECT m.* FROM tenant_members m JOIN tenants t ON t.id=m.tenant_id "
                      "WHERE m.id=? AND m.tenant_id=? AND m.status='active' AND t.active=1", (mid, tid)).fetchone()
    return _member(r) if r else None


def seat_drift() -> list[dict]:
    """Subscribed tenants on a per-seat plan whose last-synced seat count differs from the current one (or was
    never synced)."""
    from product.plans import get_plan
    with conn() as c:
        rows = c.execute("SELECT id, plan, seats_synced FROM tenants WHERE stripe_subscription_id IS NOT NULL "
                         "AND active=1").fetchall()
    out = []
    for r in rows:
        if not get_plan(r["plan"]).seat_cents:  # seats are not billed on this plan: nothing to keep in step
            continue
        n = seat_count(r["id"])
        if r["seats_synced"] != n:
            out.append({"tenant_id": r["id"], "synced": r["seats_synced"], "actual": n})
    return out
