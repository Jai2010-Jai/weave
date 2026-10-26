"""Durable SMS state in SQLite (data/sms.sqlite3): users, projects, conversation, build queue, outbox, links.

SQLite in WAL mode is the queue's durable store: Genesis is a single-machine app (one Gradle toolchain), so
this avoids running Redis while still surviving restarts. Jobs and outbound messages are claimed with
time-limited leases, so a crashed worker's work is picked up again once its lease expires."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .accounts_store import AccountsMixin

REF_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
ACTIVE_JOB_STATES = ("queued", "running")
STATUS_PRECEDENCE: dict[str, int] = {
    "pending": 0,
    "sending": 1,
    "sent": 2,
    "delivered": 3,
    "read": 4,
    "failed": 5,
    "undelivered": 5,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    phone TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL,                      -- owner | member | guest
    channel TEXT NOT NULL DEFAULT 'sms',     -- sms | whatsapp | web: where replies go
    reply_from TEXT,                         -- our number the user last wrote to (E.164); replies come from it
    status TEXT NOT NULL DEFAULT 'active',   -- active | blocked | opted_out
    active_project_id INTEGER,
    awaiting_new INTEGER NOT NULL DEFAULT 0, -- texted NEW without a description
    created_at REAL NOT NULL,
    linked_at REAL
);
CREATE TABLE IF NOT EXISTS unknown_senders (
    phone TEXT PRIMARY KEY,
    link_failures INTEGER NOT NULL DEFAULT 0,
    locked_until REAL NOT NULL DEFAULT 0,
    last_reply_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS link_codes (
    code_hash TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL,
    used_by INTEGER
);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    number INTEGER NOT NULL,                 -- 1, 2, 3... per user (what the user types in USE <n>)
    name TEXT,
    original_prompt TEXT NOT NULL,
    changes TEXT NOT NULL DEFAULT '[]',      -- JSON list of change requests, oldest first
    last_success_build_id TEXT,              -- Genesis build id (data/builds/<id>)
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (user_id, number)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    user_id INTEGER,
    phone TEXT NOT NULL,
    direction TEXT NOT NULL,                 -- in | out
    body TEXT NOT NULL,
    provider_sid TEXT,
    status TEXT NOT NULL,                    -- in: received; out: pending|sending|sent|delivered|undelivered|failed
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    lease_until REAL NOT NULL DEFAULT 0,
    job_id INTEGER,
    sender TEXT,                             -- outbound: our E.164 number to send from (NULL = configured default)
    media_id TEXT,                           -- Meta media ID or external media reference
    media_type TEXT,                         -- MIME type or media format
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS messages_sid ON messages(direction, provider_sid) WHERE provider_sid IS NOT NULL;
CREATE INDEX IF NOT EXISTS messages_outbox ON messages(direction, status, next_attempt_at);
CREATE INDEX IF NOT EXISTS messages_phone ON messages(phone, created_at);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    ref TEXT NOT NULL UNIQUE,                -- G-XXXXX, shown to the user
    user_id INTEGER NOT NULL REFERENCES users(id),
    project_id INTEGER NOT NULL REFERENCES projects(id),
    kind TEXT NOT NULL,                      -- new | change | publish (websites, owner-approved)
    prompt TEXT NOT NULL,
    change_request TEXT,
    base_build_id TEXT,
    status TEXT NOT NULL,                    -- queued | running | succeeded | failed | cancelled
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    next_run_at REAL NOT NULL,
    lease_owner TEXT,
    lease_until REAL NOT NULL DEFAULT 0,
    build_id TEXT,
    error TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    last_progress_key TEXT,
    last_progress_at REAL NOT NULL DEFAULT 0,
    inbound_sid TEXT UNIQUE,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, next_run_at);
CREATE INDEX IF NOT EXISTS jobs_user ON jobs(user_id, created_at);
CREATE TABLE IF NOT EXISTS web_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS download_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    build_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    downloads INTEGER NOT NULL DEFAULT 0
);
"""


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class SmsStore(AccountsMixin):
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._conn().executescript(SCHEMA)  # idempotent; executescript manages its own transaction
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        c = self._conn()
        cols = {r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()}
        if "channel" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN channel TEXT NOT NULL DEFAULT 'sms'")
        if "reply_from" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN reply_from TEXT")
        if "pending_text" not in cols:  # a message waiting for "new app or change?" to be answered
            c.execute("ALTER TABLE users ADD COLUMN pending_text TEXT")
        if "pending_question" not in cols:  # what pending_text is waiting on: NULL (new or change?) | kind | style | publish
            c.execute("ALTER TABLE users ADD COLUMN pending_question TEXT")
        if "referral_code" not in cols:  # this user's shareable code; friends text JOIN <code> to sign up
            c.execute("ALTER TABLE users ADD COLUMN referral_code TEXT")
        if "referred_by" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
        if "bonus_builds" not in cols:  # extra daily builds earned by referring friends
            c.execute("ALTER TABLE users ADD COLUMN bonus_builds INTEGER NOT NULL DEFAULT 0")
        if "language" not in cols:  # genesis.sms.language code (hi, te-Latn, ...); NULL = English
            c.execute("ALTER TABLE users ADD COLUMN language TEXT")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_referral_code ON users(referral_code)")
        mcols = {r["name"] for r in c.execute("PRAGMA table_info(messages)").fetchall()}
        if "sender" not in mcols:
            c.execute("ALTER TABLE messages ADD COLUMN sender TEXT")
        if "media_id" not in mcols:
            c.execute("ALTER TABLE messages ADD COLUMN media_id TEXT")
        if "media_type" not in mcols:
            c.execute("ALTER TABLE messages ADD COLUMN media_type TEXT")
        # Unified projects (Android apps and websites). Additive only: older Genesis versions ignore these columns.
        pcols = {r["name"] for r in c.execute("PRAGMA table_info(projects)").fetchall()}
        if "kind" not in pcols:  # android | website
            c.execute("ALTER TABLE projects ADD COLUMN kind TEXT NOT NULL DEFAULT 'android'")
        if "deploy_url" not in pcols:  # public URL of the last approved publish (websites only)
            c.execute("ALTER TABLE projects ADD COLUMN deploy_url TEXT")
        if "deploy_site_id" not in pcols:
            c.execute("ALTER TABLE projects ADD COLUMN deploy_site_id TEXT")
        self._migrate_accounts(c)

    # ---------- connection ----------
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """One write transaction. BEGIN IMMEDIATE serializes writers across threads and processes."""
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        c.execute("COMMIT")

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        row = self._conn().execute(sql, args).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, args: tuple = ()) -> list[dict]:
        return [dict(r) for r in self._conn().execute(sql, args).fetchall()]

    # ---------- users ----------
    def get_user(self, phone: str) -> dict | None:
        return self.one("SELECT * FROM users WHERE phone = ?", (phone,))

    def user_by_id(self, user_id: int) -> dict | None:
        return self.one("SELECT * FROM users WHERE id = ?", (user_id,))

    def create_user(self, phone: str, role: str, channel: str = "sms") -> dict:
        now = time.time()
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO users (phone, role, channel, created_at, linked_at) VALUES (?, ?, ?, ?, ?)",
                      (phone, role, channel, now, now))
        return self.get_user(phone)  # type: ignore[return-value]

    def update_user(self, user_id: int, **fields: Any) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE users SET {cols} WHERE id = ?", (*fields.values(), user_id))

    def unknown_sender(self, phone: str) -> dict:
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO unknown_senders (phone) VALUES (?)", (phone,))
        return self.one("SELECT * FROM unknown_senders WHERE phone = ?", (phone,))  # type: ignore[return-value]

    def update_unknown_sender(self, phone: str, **fields: Any) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE unknown_senders SET {cols} WHERE phone = ?", (*fields.values(), phone))

    # ---------- link codes ----------
    def create_link_code(self, role: str = "member", ttl_s: int = 900) -> str:
        raw = "".join(secrets.choice(REF_ALPHABET) for _ in range(10))
        now = time.time()
        with self.tx() as c:
            c.execute("INSERT INTO link_codes (code_hash, role, created_at, expires_at) VALUES (?, ?, ?, ?)",
                      (sha256(raw), role, now, now + ttl_s))
        return raw

    def consume_link_code(self, raw: str, phone: str, channel: str = "sms") -> dict | None:
        """Atomically use a one-time code and create the user. Returns the user, or None if invalid."""
        code = "".join(ch for ch in raw.upper() if ch.isalnum())
        now = time.time()
        with self.tx() as c:
            row = c.execute("SELECT * FROM link_codes WHERE code_hash = ? AND used_at IS NULL AND expires_at > ?",
                            (sha256(code), now)).fetchone()
            if not row:
                return None
            c.execute("INSERT OR IGNORE INTO users (phone, role, channel, created_at, linked_at) VALUES (?, ?, ?, ?, ?)",
                      (phone, row["role"], channel, now, now))
            user = c.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
            c.execute("UPDATE link_codes SET used_at = ?, used_by = ? WHERE code_hash = ?", (now, user["id"], row["code_hash"]))
        return dict(user)

    # ---------- referrals ----------
    def referral_code(self, user_id: int) -> str:
        """The user's shareable referral code, created on first use."""
        row = self.one("SELECT referral_code FROM users WHERE id = ?", (user_id,))
        if row and row["referral_code"]:
            return row["referral_code"]
        for _ in range(20):
            code = "".join(secrets.choice(REF_ALPHABET) for _ in range(6))
            try:
                with self.tx() as c:
                    c.execute("UPDATE users SET referral_code = ? WHERE id = ? AND referral_code IS NULL", (code, user_id))
            except sqlite3.IntegrityError:
                continue
            return self.one("SELECT referral_code FROM users WHERE id = ?", (user_id,))["referral_code"]
        raise RuntimeError("could not create a referral code")

    def referral_count(self, user_id: int) -> int:
        return self.one("SELECT COUNT(*) AS n FROM users WHERE referred_by = ?", (user_id,))["n"]

    def consume_referral_code(self, raw: str, phone: str, channel: str, role: str, bonus: int,
                              max_referrals: int) -> tuple[dict, dict] | None:
        """Atomically sign up a new user with a friend's code and credit the friend.
        Returns (new_user, referrer), or None if the code is unknown, its owner is inactive or out of invites."""
        code = "".join(ch for ch in raw.upper() if ch.isalnum())
        if not code:
            return None
        now = time.time()
        with self.tx() as c:
            ref = c.execute("SELECT * FROM users WHERE referral_code = ? AND status = 'active'", (code,)).fetchone()
            if not ref or ref["phone"] == phone:
                return None
            used = c.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (ref["id"],)).fetchone()[0]
            if used >= max_referrals:
                return None
            c.execute("INSERT OR IGNORE INTO users (phone, role, channel, created_at, linked_at, referred_by) "
                      "VALUES (?, ?, ?, ?, ?, ?)", (phone, role, channel, now, now, ref["id"]))
            c.execute("UPDATE users SET bonus_builds = bonus_builds + ? WHERE id = ?", (bonus, ref["id"]))
            user = c.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
            ref = c.execute("SELECT * FROM users WHERE id = ?", (ref["id"],)).fetchone()
        return dict(user), dict(ref)

    # ---------- projects ----------
    def create_project(self, user_id: int, prompt: str, kind: str = "android") -> dict:
        if kind not in ("android", "website"):
            raise ValueError(f"unknown project kind: {kind}")
        now = time.time()
        with self.tx() as c:
            n = c.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM projects WHERE user_id = ?", (user_id,)).fetchone()[0]
            cur = c.execute("INSERT INTO projects (user_id, number, kind, original_prompt, created_at, updated_at) "
                            "VALUES (?, ?, ?, ?, ?, ?)", (user_id, n, kind, prompt, now, now))
            c.execute("UPDATE users SET active_project_id = ?, awaiting_new = 0 WHERE id = ?", (cur.lastrowid, user_id))
        return self.project(user_id, cur.lastrowid)  # type: ignore[return-value]

    def project(self, user_id: int, project_id: int | None) -> dict | None:
        """Projects are always looked up together with their owner: the authorization boundary."""
        if project_id is None:
            return None
        return self.one("SELECT * FROM projects WHERE id = ? AND user_id = ?", (project_id, user_id))

    def project_by_number(self, user_id: int, number: int) -> dict | None:
        return self.one("SELECT * FROM projects WHERE user_id = ? AND number = ? AND deleted_at IS NULL", (user_id, number))

    def projects(self, user_id: int, limit: int = 8) -> list[dict]:
        return self.all("SELECT * FROM projects WHERE user_id = ? AND deleted_at IS NULL ORDER BY updated_at DESC LIMIT ?",
                        (user_id, limit))

    def update_project(self, project_id: int, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE projects SET {cols} WHERE id = ?", (*fields.values(), project_id))

    def add_change(self, project_id: int, change: str) -> None:
        with self.tx() as c:
            row = c.execute("SELECT changes FROM projects WHERE id = ?", (project_id,)).fetchone()
            changes = json.loads(row["changes"]) + [change]
            c.execute("UPDATE projects SET changes = ?, updated_at = ? WHERE id = ?",
                      (json.dumps(changes), time.time(), project_id))

    # ---------- inbound / conversation ----------
    def enqueue_inbound(
        self,
        phone: str,
        body: str,
        sid: str,
        sender: str | None = None,
        media_id: str | None = None,
        media_type: str | None = None,
    ) -> bool:
        """Durably enqueue an inbound message for background worker processing.
        Returns True if newly inserted, False if sid already exists (duplicate delivery)."""
        now = time.time()
        with self.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO messages (phone, direction, body, provider_sid, status, "
                "sender, media_id, media_type, attempts, next_attempt_at, created_at, updated_at) "
                "VALUES (?, 'in', ?, ?, 'pending', ?, ?, ?, 0, ?, ?, ?)",
                (phone, body, sid, sender, media_id, media_type, now, now, now)
            )
            return cur.rowcount == 1

    def update_inbound_body(self, msg_id: int, body: str) -> None:
        """Update the message body (e.g. after transcribing a voice note)."""
        with self.tx() as c:
            c.execute("UPDATE messages SET body = ?, updated_at = ? WHERE id = ?",
                      (body[:1600], time.time(), msg_id))

    def claim_inbound(self, lease_s: float = 60, limit: int = 10) -> list[dict]:
        """Claim pending inbound messages for background processing."""
        now = time.time()
        with self.tx() as c:
            rows = c.execute(
                "SELECT * FROM messages WHERE direction = 'in' AND "
                "((status = 'pending' AND next_attempt_at <= ?) OR (status = 'processing' AND lease_until < ?)) "
                "ORDER BY id LIMIT ?",
                (now, now, limit)
            ).fetchall()
            for r in rows:
                c.execute(
                    "UPDATE messages SET status = 'processing', lease_until = ?, attempts = attempts + 1, updated_at = ? "
                    "WHERE id = ?",
                    (now + lease_s, now, r["id"])
                )
        return [dict(r) for r in rows]

    def inbound_done(self, msg_id: int, user_id: int | None = None) -> None:
        """Mark an inbound message as successfully processed."""
        with self.tx() as c:
            c.execute(
                "UPDATE messages SET status = 'received', user_id = COALESCE(?, user_id), error = NULL, updated_at = ? "
                "WHERE id = ?",
                (user_id, time.time(), msg_id)
            )

    def inbound_failed(self, msg_id: int, error: str, retry_in: float | None) -> None:
        """Record an inbound processing failure, scheduling a retry if retry_in is given."""
        now = time.time()
        with self.tx() as c:
            if retry_in is None:
                c.execute("UPDATE messages SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                          (error[:500], now, msg_id))
            else:
                c.execute("UPDATE messages SET status = 'pending', error = ?, next_attempt_at = ?, updated_at = ? WHERE id = ?",
                          (error[:500], now + retry_in, now, msg_id))

    def record_inbound(self, phone: str, body: str, sid: str, user_id: int | None) -> bool:
        """Store an inbound message. False if this MessageSid was already processed (duplicate delivery)."""
        now = time.time()
        with self.tx() as c:
            row = c.execute("SELECT id, status FROM messages WHERE direction = 'in' AND provider_sid = ?", (sid,)).fetchone()
            if row:
                if row["status"] in ("pending", "processing"):
                    c.execute("UPDATE messages SET user_id = COALESCE(?, user_id), updated_at = ? WHERE id = ?",
                              (user_id, now, row["id"]))
                    return True
                return False
            cur = c.execute("INSERT OR IGNORE INTO messages (user_id, phone, direction, body, provider_sid, status, "
                            "created_at, updated_at) VALUES (?, ?, 'in', ?, ?, 'received', ?, ?)",
                            (user_id, phone, body, sid, now, now))
            return cur.rowcount == 1

    def has_inbound(self, sid: str) -> bool:
        return self.one("SELECT 1 FROM messages WHERE direction = 'in' AND provider_sid = ?", (sid,)) is not None

    def set_inbound_user(self, sid: str, user_id: int) -> None:
        with self.tx() as c:
            c.execute("UPDATE messages SET user_id = ? WHERE direction = 'in' AND provider_sid = ?", (user_id, sid))

    def inbound_count(self, phone: str, since: float) -> int:
        row = self.one("SELECT COUNT(*) AS n FROM messages WHERE phone = ? AND direction = 'in' AND created_at > ?",
                       (phone, since))
        return row["n"] if row else 0

    def conversation(self, user_id: int, limit: int = 20) -> list[dict]:
        rows = self.all("SELECT direction, body, status, created_at FROM messages WHERE user_id = ? "
                        "ORDER BY id DESC LIMIT ?", (user_id, limit))
        return list(reversed(rows))

    # ---------- outbox ----------
    def enqueue_sms(self, phone: str, body: str, user_id: int | None, job_id: int | None = None,
                    sender: str | None = None, status: str = "pending") -> int:
        """status 'localizing': held back until release_outbound puts the translated text in."""
        now = time.time()
        with self.tx() as c:
            cur = c.execute("INSERT INTO messages (user_id, phone, direction, body, status, next_attempt_at, job_id, "
                            "sender, created_at, updated_at) VALUES (?, ?, 'out', ?, ?, ?, ?, ?, ?, ?)",
                            (user_id, phone, body, status, now, job_id, sender, now, now))
            return cur.lastrowid  # type: ignore[return-value]

    def release_outbound(self, msg_id: int, body: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE messages SET body = ?, status = 'pending', updated_at = ? WHERE id = ? AND status = 'localizing'",
                      (body, time.time(), msg_id))

    def claim_outbound(self, lease_s: float = 60, limit: int = 10) -> list[dict]:
        now = time.time()
        with self.tx() as c:
            # A translation that never came back (crash, restart) goes out in English after a minute.
            rows = c.execute("SELECT * FROM messages WHERE direction = 'out' AND ((status = 'pending' AND next_attempt_at <= ?)"
                             " OR (status = 'sending' AND lease_until < ?) OR (status = 'localizing' AND updated_at < ?))"
                             " ORDER BY id LIMIT ?", (now, now, now - 60, limit)).fetchall()
            for r in rows:
                c.execute("UPDATE messages SET status = 'sending', lease_until = ?, attempts = attempts + 1, updated_at = ? "
                          "WHERE id = ?", (now + lease_s, now, r["id"]))
        return [dict(r) for r in rows]

    def outbound_sent(self, msg_id: int, sid: str, status: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE messages SET status = ?, provider_sid = ?, error = NULL, updated_at = ? WHERE id = ?",
                      (status or "sent", sid, time.time(), msg_id))

    def outbound_failed(self, msg_id: int, error: str, retry_in: float | None) -> None:
        now = time.time()
        with self.tx() as c:
            if retry_in is None:
                c.execute("UPDATE messages SET status = 'failed', error = ?, updated_at = ? WHERE id = ?", (error[:500], now, msg_id))
            else:
                c.execute("UPDATE messages SET status = 'pending', error = ?, next_attempt_at = ?, updated_at = ? WHERE id = ?",
                          (error[:500], now + retry_in, now, msg_id))

    def delivery_status(self, sid: str, status: str, error_code: str | None) -> bool:
        with self.tx() as c:
            row = c.execute("SELECT id, status FROM messages WHERE direction = 'out' AND provider_sid = ?", (sid,)).fetchone()
            if not row:
                return False
            current_status = row["status"]
            current_rank = STATUS_PRECEDENCE.get(current_status, 0)
            new_rank = STATUS_PRECEDENCE.get(status, 0)
            target_status = status if new_rank >= current_rank else current_status
            c.execute("UPDATE messages SET status = ?, error = COALESCE(?, error), updated_at = ? WHERE id = ?",
                      (target_status, error_code, time.time(), row["id"]))
            return True

    # ---------- jobs ----------
    def _new_ref(self, c: sqlite3.Connection) -> str:
        while True:
            ref = "G-" + "".join(secrets.choice(REF_ALPHABET) for _ in range(5))
            if not c.execute("SELECT 1 FROM jobs WHERE ref = ?", (ref,)).fetchone():
                return ref

    def enqueue_job(self, *, user_id: int, project_id: int, kind: str, prompt: str, change_request: str | None,
                    base_build_id: str | None, max_attempts: int, inbound_sid: str | None) -> dict:
        now = time.time()
        with self.tx() as c:
            if inbound_sid:
                existing = c.execute("SELECT * FROM jobs WHERE inbound_sid = ?", (inbound_sid,)).fetchone()
                if existing:
                    return dict(existing)
            ref = self._new_ref(c)
            cur = c.execute("INSERT INTO jobs (ref, user_id, project_id, kind, prompt, change_request, base_build_id, status, "
                            "max_attempts, next_run_at, inbound_sid, created_at, updated_at, last_progress_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)",
                            (ref, user_id, project_id, kind, prompt, change_request, base_build_id, max_attempts, now,
                             inbound_sid, now, now, now))
        return self.job(cur.lastrowid)  # type: ignore[return-value]

    def job(self, job_id: int) -> dict | None:
        return self.one("SELECT * FROM jobs WHERE id = ?", (job_id,))

    def job_for_user(self, user_id: int, ref: str) -> dict | None:
        return self.one("SELECT * FROM jobs WHERE user_id = ? AND ref = ?", (user_id, ref.upper()))

    def active_job(self, user_id: int) -> dict | None:
        return self.one("SELECT * FROM jobs WHERE user_id = ? AND status IN ('queued', 'running') ORDER BY id DESC LIMIT 1",
                        (user_id,))

    def latest_job(self, user_id: int, project_id: int) -> dict | None:
        return self.one("SELECT * FROM jobs WHERE user_id = ? AND project_id = ? ORDER BY id DESC LIMIT 1",
                        (user_id, project_id))

    def jobs_since(self, user_id: int, since: float) -> int:
        row = self.one("SELECT COUNT(*) AS n FROM jobs WHERE user_id = ? AND created_at > ?", (user_id, since))
        return row["n"] if row else 0

    def queued_total(self) -> int:
        row = self.one("SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued', 'running')")
        return row["n"] if row else 0

    def claim_job(self, worker_id: str, lease_s: float, lane: str | None = None) -> dict | None:
        """Take the oldest runnable job. A user never has two jobs running at once.
        lane: 'website' (website builds and publishes), 'android', or None for any job."""
        now = time.time()
        lane_sql = {None: "", "website": " AND p.kind = 'website'", "android": " AND p.kind != 'website'"}[lane]
        with self.tx() as c:
            row = c.execute("SELECT j.* FROM jobs j JOIN projects p ON p.id = j.project_id "
                            "WHERE j.status = 'queued' AND j.next_run_at <= ?" + lane_sql + " AND NOT EXISTS "
                            "(SELECT 1 FROM jobs r WHERE r.user_id = j.user_id AND r.status = 'running') "
                            "ORDER BY j.next_run_at, j.id LIMIT 1", (now,)).fetchone()
            if not row:
                return None
            c.execute("UPDATE jobs SET status = 'running', lease_owner = ?, lease_until = ?, attempts = attempts + 1, "
                      "updated_at = ? WHERE id = ?", (worker_id, now + lease_s, now, row["id"]))
        return self.job(row["id"])

    def heartbeat(self, job_id: int, worker_id: str, lease_s: float) -> dict | None:
        with self.tx() as c:
            c.execute("UPDATE jobs SET lease_until = ? WHERE id = ? AND lease_owner = ? AND status = 'running'",
                      (time.time() + lease_s, job_id, worker_id))
        return self.job(job_id)

    def update_job(self, job_id: int, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        if fields.get("status") in ("succeeded", "failed", "cancelled"):
            fields["finished_at"] = fields["updated_at"]
            fields["lease_owner"] = None
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))

    def requeue(self, job_id: int, delay_s: float, error: str) -> None:
        self.update_job(job_id, status="queued", next_run_at=time.time() + delay_s, lease_owner=None, lease_until=0,
                        build_id=None, error=error[:1000])

    def expired_jobs(self) -> list[dict]:
        return self.all("SELECT * FROM jobs WHERE status = 'running' AND lease_until < ?", (time.time(),))

    def request_cancel(self, job_id: int) -> str:
        """Queued jobs are cancelled immediately; running ones are flagged for the worker. Returns the new state."""
        now = time.time()
        with self.tx() as c:
            row = c.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row["status"] == "queued":
                c.execute("UPDATE jobs SET status = 'cancelled', cancel_requested = 1, finished_at = ?, updated_at = ? "
                          "WHERE id = ?", (now, now, job_id))
                return "cancelled"
            if row["status"] == "running":
                c.execute("UPDATE jobs SET cancel_requested = 1, updated_at = ? WHERE id = ?", (now, job_id))
                return "cancelling"
            return row["status"]

    # ---------- download tokens ----------
    def create_download_token(self, user_id: int, build_id: str, ttl_s: float) -> str:
        raw = secrets.token_urlsafe(24)
        now = time.time()
        with self.tx() as c:
            c.execute("INSERT INTO download_tokens (token_hash, user_id, build_id, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                      (sha256(raw), user_id, build_id, now, now + ttl_s))
        return raw

    def use_download_token(self, raw: str, max_downloads: int) -> tuple[str, dict | None]:
        """Returns ('ok', row) | ('expired', row) | ('exhausted', row) | ('unknown', None)."""
        with self.tx() as c:
            row = c.execute("SELECT * FROM download_tokens WHERE token_hash = ?", (sha256(raw),)).fetchone()
            if not row:
                return "unknown", None
            if row["expires_at"] <= time.time():
                return "expired", dict(row)
            if row["downloads"] >= max_downloads:
                return "exhausted", dict(row)
            c.execute("UPDATE download_tokens SET downloads = downloads + 1 WHERE token_hash = ?", (row["token_hash"],))
        return "ok", dict(row)

    # ---------- web chat sessions ----------
    def create_web_session(self, user_id: int, ttl_s: float) -> str:
        raw = secrets.token_urlsafe(32)
        now = time.time()
        with self.tx() as c:
            c.execute("DELETE FROM web_sessions WHERE expires_at < ?", (now,))
            c.execute("INSERT INTO web_sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                      (sha256(raw), user_id, now, now + ttl_s))
        return raw

    def web_session_user(self, raw: str) -> dict | None:
        return self.one("SELECT u.* FROM web_sessions s JOIN users u ON u.id = s.user_id "
                        "WHERE s.token_hash = ? AND s.expires_at > ? AND u.status = 'active'", (sha256(raw), time.time()))

    def delete_web_session(self, raw: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM web_sessions WHERE token_hash = ?", (sha256(raw),))

    def messages_for_user(self, user_id: int, after_id: int = 0, limit: int = 200) -> list[dict]:
        rows = self.all("SELECT id, direction, body, status, created_at FROM messages WHERE user_id = ? AND id > ? "
                        "ORDER BY id DESC LIMIT ?", (user_id, after_id, limit))
        return list(reversed(rows))

    def stats(self) -> dict:
        jobs = {r["status"]: r["n"] for r in self.all("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}
        out = {r["status"]: r["n"] for r in self.all("SELECT status, COUNT(*) AS n FROM messages WHERE direction = 'out' GROUP BY status")}
        users = self.one("SELECT COUNT(*) AS n FROM users")
        return {"users": users["n"] if users else 0, "jobs": jobs, "outbound": out}
