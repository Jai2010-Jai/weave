"""Weave accounts: the people behind phone numbers.

An account (made on the website, by email + password or Google) owns a permanent username, which is its
subdomain (<username>.askweave.xyz), and a call sign the bot uses for them. One WhatsApp number is linked to one
account by texting LINK <code>; that number's user row then carries account_id, and every project it builds is
hosted at <username>.askweave.xyz/<project>/. Mixed into SmsStore so accounts share its database and transactions."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Any

ACCOUNTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,           -- permanent; the subdomain
    call_sign TEXT NOT NULL,                 -- what the bot calls them; editable
    email TEXT UNIQUE,
    google_sub TEXT UNIQUE,
    password_hash TEXT,
    referral_code TEXT,                      -- a friend's invite code given at sign-up
    status TEXT NOT NULL DEFAULT 'active',   -- active | suspended | deleted
    created_at REAL NOT NULL,
    terms_accepted_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS account_sessions (
    token_hash TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS phone_codes (
    code_hash TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL
);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY,
    site TEXT NOT NULL,
    reason TEXT NOT NULL,
    details TEXT,
    status TEXT NOT NULL DEFAULT 'open',     -- open | removed | dismissed
    created_at REAL NOT NULL
);
"""
PHONE_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str | None) -> bool:
    try:
        kind, salt, digest = (stored or "").split("$")
    except ValueError:
        return False
    if kind != "scrypt":
        return False
    got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(got.hex(), digest)


class AccountsMixin:
    """Account methods for SmsStore (uses its tx/one/all helpers)."""

    def _migrate_accounts(self, c) -> None:
        c.executescript(ACCOUNTS_SCHEMA)
        ucols = {r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()}
        if "account_id" not in ucols:
            c.execute("ALTER TABLE users ADD COLUMN account_id INTEGER")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_account ON users(account_id) WHERE account_id IS NOT NULL")
        pcols = {r["name"] for r in c.execute("PRAGMA table_info(projects)").fetchall()}
        if "slug" not in pcols:  # path under the owner's subdomain: <username>.<domain>/<slug>/
            c.execute("ALTER TABLE projects ADD COLUMN slug TEXT")
        if "deleted_at" not in pcols:  # soft delete: restorable until the trash is emptied
            c.execute("ALTER TABLE projects ADD COLUMN deleted_at REAL")
            c.execute("ALTER TABLE projects ADD COLUMN trash_name TEXT")
        jcols = {r["name"] for r in c.execute("PRAGMA table_info(jobs)").fetchall()}
        if "tokens_in" not in jcols:  # Gemini usage of the job, for per-user cost tracking
            c.execute("ALTER TABLE jobs ADD COLUMN tokens_in INTEGER NOT NULL DEFAULT 0")
            c.execute("ALTER TABLE jobs ADD COLUMN tokens_out INTEGER NOT NULL DEFAULT 0")

    # ---------- accounts ----------
    def create_account(self, *, username: str, call_sign: str, email: str | None = None, password: str | None = None,
                       google_sub: str | None = None, referral_code: str | None = None) -> dict:
        now = time.time()
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO accounts (username, call_sign, email, google_sub, password_hash, referral_code, created_at, "
                "terms_accepted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (username, call_sign, email, google_sub, hash_password(password) if password else None,
                 referral_code, now, now))
        return self.account(cur.lastrowid)  # type: ignore[return-value]

    def account(self, account_id: int | None) -> dict | None:
        if account_id is None:
            return None
        return self.one("SELECT * FROM accounts WHERE id = ?", (account_id,))

    def account_by(self, field: str, value: str) -> dict | None:
        if field not in ("username", "email", "google_sub"):
            raise ValueError(field)
        return self.one(f"SELECT * FROM accounts WHERE {field} = ?", (value,))

    def update_account(self, account_id: int, **fields: Any) -> None:
        if "password" in fields:
            fields["password_hash"] = hash_password(fields.pop("password"))
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE accounts SET {cols} WHERE id = ?", (*fields.values(), account_id))

    def account_user(self, account_id: int) -> dict | None:
        """The phone identity (users row) linked to an account, if any."""
        return self.one("SELECT * FROM users WHERE account_id = ?", (account_id,))

    def account_for_user(self, user_id: int) -> dict | None:
        return self.one("SELECT a.* FROM accounts a JOIN users u ON u.account_id = a.id "
                        "WHERE u.id = ? AND a.status = 'active'", (user_id,))

    # ---------- sessions ----------
    def create_account_session(self, account_id: int, ttl_s: float) -> str:
        raw = secrets.token_urlsafe(32)
        now = time.time()
        with self.tx() as c:
            c.execute("INSERT INTO account_sessions (token_hash, account_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                      (_hash(raw), account_id, now, now + ttl_s))
        return raw

    def account_by_session(self, raw: str) -> dict | None:
        return self.one("SELECT a.* FROM account_sessions s JOIN accounts a ON a.id = s.account_id "
                        "WHERE s.token_hash = ? AND s.expires_at > ? AND a.status = 'active'", (_hash(raw), time.time()))

    def delete_account_sessions(self, account_id: int | None = None, raw: str | None = None) -> None:
        with self.tx() as c:
            if raw is not None:
                c.execute("DELETE FROM account_sessions WHERE token_hash = ?", (_hash(raw),))
            if account_id is not None:
                c.execute("DELETE FROM account_sessions WHERE account_id = ?", (account_id,))

    # ---------- linking a phone number ----------
    def create_phone_code(self, account_id: int, ttl_s: float = 3600) -> str:
        code = "".join(secrets.choice(PHONE_CODE_ALPHABET) for _ in range(6))
        now = time.time()
        with self.tx() as c:
            c.execute("DELETE FROM phone_codes WHERE account_id = ? AND used_at IS NULL", (account_id,))
            c.execute("INSERT INTO phone_codes (code_hash, account_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                      (_hash(code), account_id, now, now + ttl_s))
        return code

    def link_phone(self, raw_code: str, phone: str, channel: str) -> tuple[str, dict | None, dict | None]:
        """Use a phone code: link `phone` to the code's account. Returns (outcome, user, account).
        outcome: linked | relinked (number changed, projects kept) | taken (number is another account's) | invalid.
        One account has one number and one number one account."""
        code = "".join(ch for ch in raw_code.upper() if ch.isalnum())
        now = time.time()
        with self.tx() as c:
            row = c.execute("SELECT * FROM phone_codes WHERE code_hash = ? AND used_at IS NULL AND expires_at > ?",
                            (_hash(code), now)).fetchone()
            account = row and c.execute("SELECT * FROM accounts WHERE id = ? AND status = 'active'",
                                        (row["account_id"],)).fetchone()
            if not account:
                return "invalid", None, None
            current = c.execute("SELECT * FROM users WHERE account_id = ?", (account["id"],)).fetchone()
            existing = c.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
            if existing and existing["account_id"] not in (None, account["id"]):
                return "taken", None, dict(account)
            outcome = "linked"
            if current and current["phone"] != phone:
                outcome = "relinked"
                if existing:
                    # The new number already used Weave on its own: its projects join the account's.
                    for table in ("projects", "jobs"):
                        c.execute(f"UPDATE {table} SET user_id = ? WHERE user_id = ?", (current["id"], existing["id"]))
                    c.execute("UPDATE users SET phone = ?, status = 'blocked' WHERE id = ?",
                              (f"merged:{existing['id']}:{existing['phone']}", existing["id"]))
                c.execute("UPDATE users SET phone = ?, channel = ? WHERE id = ?", (phone, channel, current["id"]))
                user_id = current["id"]
            elif existing:
                c.execute("UPDATE users SET account_id = ?, status = 'active' WHERE id = ?", (account["id"], existing["id"]))
                user_id = existing["id"]
            else:
                cur = c.execute("INSERT INTO users (phone, role, channel, created_at, linked_at, account_id) "
                                "VALUES (?, 'member', ?, ?, ?, ?)", (phone, channel, now, now, account["id"]))
                user_id = cur.lastrowid
                outcome = "created"
            c.execute("UPDATE phone_codes SET used_at = ? WHERE code_hash = ?", (now, row["code_hash"]))
            user = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return outcome, dict(user), dict(account)

    def credit_referral(self, user_id: int, raw: str, bonus: int, max_referrals: int) -> dict | None:
        """Credit the friend whose invite code an account signed up with. Returns the referrer, or None."""
        code = "".join(ch for ch in (raw or "").upper() if ch.isalnum())
        if not code:
            return None
        with self.tx() as c:
            ref = c.execute("SELECT * FROM users WHERE referral_code = ? AND status = 'active' AND id != ?",
                            (code, user_id)).fetchone()
            if not ref or c.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (ref["id"],)).fetchone()[0] >= max_referrals:
                return None
            c.execute("UPDATE users SET referred_by = ? WHERE id = ? AND referred_by IS NULL", (ref["id"], user_id))
            c.execute("UPDATE users SET bonus_builds = bonus_builds + ? WHERE id = ?", (bonus, ref["id"]))
        return dict(ref)

    # ---------- projects and usage ----------
    def account_projects(self, user_id: int, deleted: bool = False) -> list[dict]:
        cond = "deleted_at IS NOT NULL" if deleted else "deleted_at IS NULL"
        return self.all(f"SELECT * FROM projects WHERE user_id = ? AND {cond} ORDER BY updated_at DESC", (user_id,))

    def slug_taken(self, user_id: int, slug: str, project_id: int) -> bool:
        return self.one("SELECT 1 AS x FROM projects WHERE user_id = ? AND slug = ? AND id != ?",
                        (user_id, slug, project_id)) is not None

    def usage(self, user_id: int, since: float = 0) -> dict:
        row = self.one("SELECT COUNT(*) AS builds, COALESCE(SUM(tokens_in), 0) AS tokens_in, "
                       "COALESCE(SUM(tokens_out), 0) AS tokens_out FROM jobs WHERE user_id = ? AND created_at >= ?",
                       (user_id, since))
        return row or {"builds": 0, "tokens_in": 0, "tokens_out": 0}

    def project_by_slug(self, user_id: int, slug: str) -> dict | None:
        return self.one("SELECT * FROM projects WHERE user_id = ? AND slug = ? AND deleted_at IS NULL", (user_id, slug))

    # ---------- backups ----------
    def backup(self, folder, keep: int = 7):
        """A consistent copy of the database (SQLite online backup), keeping the newest `keep` copies."""
        import sqlite3
        from pathlib import Path

        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / time.strftime("sms-%Y%m%d-%H%M%S.sqlite3")
        out = sqlite3.connect(dest)
        try:
            self._conn().backup(out)
        finally:
            out.close()
        for old in sorted(folder.glob("sms-*.sqlite3"))[:-keep]:
            old.unlink(missing_ok=True)
        return dest

    # ---------- reports ----------
    def add_report(self, site: str, reason: str, details: str) -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO reports (site, reason, details, created_at) VALUES (?, ?, ?, ?)",
                            (site, reason, details, time.time()))
        return cur.lastrowid

    def close_report(self, report_id: int, status: str = "closed") -> None:
        with self.tx() as c:
            c.execute("UPDATE reports SET status = ? WHERE id = ?", (status, report_id))

    def reports(self, status: str = "open") -> list[dict]:
        return self.all("SELECT * FROM reports WHERE status = ? ORDER BY id DESC LIMIT 100", (status,))
