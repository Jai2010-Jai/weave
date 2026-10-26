"""askweave.xyz accounts: sign up (email or Google), the dashboard, and abuse reports.

An account owns a permanent username (its subdomain, <username>.askweave.xyz) and an editable call sign. The
account proves it owns a WhatsApp number by texting LINK <code> from it; after that the bot and the dashboard
share the same projects. The browser holds an HttpOnly session cookie that is host-only (never shared with the
customer sites on the subdomains), and every POST needs the X-Weave header, so no other site can post here."""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from ..config import Settings
from ..core.jobs import JobStore
from ..sms import messages as M
from ..sms.chat import AttemptLimiter
from ..sms.observe import event
from ..sms.phone import mask
from ..sms.routes import PREVIEW_CSP
from ..sms.service import Inbound, SmsService, address
from ..sms.store import SmsStore
from ..web.hosting import dir_size
from ..web.validate import PAGE_NAME_RE
from .names import clean_call_sign, username_problem

STATIC = Path(__file__).resolve().parent.parent / "static" / "site"
COOKIE = "weave_session"
PENDING_COOKIE = "weave_google"
SESSION_TTL_S = 30 * 86_400
PAGES = {"start", "dashboard", "terms", "privacy", "report"}
ASSETS = {"site.css": "text/css", "site.js": "text/javascript", "start.js": "text/javascript",
          "dashboard.js": "text/javascript", "report.js": "text/javascript"}
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self' https://accounts.google.com")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[a-z]{2,}$", re.I)
REPORT_REASONS = {"phishing", "scam", "malware", "impersonation", "adult", "hate", "copyright", "other"}
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_TOKENINFO = "https://oauth2.googleapis.com/tokeninfo"

log = logging.getLogger(__name__)


class Pending:
    """Short-lived server-side state: Google sign-ins waiting to pick a username, and OAuth `state` values."""

    def __init__(self, ttl_s: float = 900):
        self.ttl, self.items, self.lock = ttl_s, {}, threading.Lock()

    def put(self, value) -> str:
        key = secrets.token_urlsafe(24)
        with self.lock:
            now = time.time()
            self.items = {k: v for k, v in self.items.items() if v[0] > now}
            self.items[key] = (now + self.ttl, value)
        return key

    def get(self, key: str, pop: bool = False):
        with self.lock:
            item = self.items.pop(key, None) if pop else self.items.get(key)
        return item[1] if item and item[0] > time.time() else None


def build_site_router(settings: Settings, store: SmsStore, service: SmsService, builds: JobStore) -> APIRouter:
    r = APIRouter()
    logins = AttemptLimiter(max_failures=30, window_s=600)
    signups = AttemptLimiter(max_failures=40, window_s=3600)  # counts every sign-up, not just failures
    reports = AttemptLimiter(max_failures=30, window_s=3600)
    google = Pending()
    states = Pending(ttl_s=600)
    base = settings.public_base_url
    domain = settings.sites_domain or "askweave.xyz"

    def secure(request: Request) -> bool:
        return (request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https"
                or base.startswith("https://") and not getattr(request.state, "local", False))

    def require_csrf(request: Request) -> None:
        if request.headers.get("x-weave") != "1":
            raise HTTPException(403, "missing X-Weave header")

    def current(request: Request) -> dict:
        token = request.cookies.get(COOKIE, "")
        account = store.account_by_session(token) if token else None
        if account is None:
            raise HTTPException(401, "Not signed in")
        return account

    def signed_in(resp: Response, request: Request, account_id: int) -> Response:
        token = store.create_account_session(account_id, SESSION_TTL_S)
        # No Domain attribute: the cookie stays on this host and never reaches <username>.askweave.xyz.
        resp.set_cookie(COOKIE, token, max_age=SESSION_TTL_S, httponly=True, samesite="lax",
                        secure=secure(request), path="/")
        resp.delete_cookie(PENDING_COOKIE, path="/")
        return resp

    async def body(request: Request) -> dict:
        try:
            data = await request.json()
        except ValueError:
            raise HTTPException(400, "send JSON") from None
        if not isinstance(data, dict):
            raise HTTPException(400, "send a JSON object")
        return data

    def project_of(account: dict, project_id: int, deleted: bool | None = False) -> tuple[dict, dict]:
        user = store.account_user(account["id"])
        project = store.project(user["id"], project_id) if user else None
        if project is None or (deleted is not None and bool(project.get("deleted_at")) != deleted):
            raise HTTPException(404, "project not found")
        return user, project

    def project_json(user: dict, p: dict) -> dict:
        last = store.latest_job(user["id"], p["id"])
        return {"id": p["id"], "number": p["number"], "name": M.name_of(p), "kind": p.get("kind") or "android",
                "url": p.get("deploy_url"), "prompt": p.get("original_prompt"), "updated_at": p.get("updated_at"),
                "status": last["status"] if last else None, "ref": last["ref"] if last else None,
                "ready": bool(p.get("last_success_build_id")), "deleted_at": p.get("deleted_at")}

    # ---------- pages ----------
    def page(name: str) -> FileResponse:
        return FileResponse(STATIC / f"{name}.html", headers={"Cache-Control": "no-cache", "Content-Security-Policy": CSP})

    @r.get("/start")
    def start_page():
        return page("start")

    @r.get("/dashboard")
    def dashboard_page():
        return page("dashboard")

    @r.get("/terms")
    def terms_page():
        return page("terms")

    @r.get("/privacy")
    def privacy_page():
        return page("privacy")

    @r.get("/report")
    def report_page():
        return page("report")

    @r.get("/site/{name}")
    def asset(name: str):
        if name not in ASSETS:
            raise HTTPException(404)
        return FileResponse(STATIC / name, media_type=ASSETS[name], headers={"Cache-Control": "no-cache"})

    # ---------- sign up and sign in ----------
    @r.get("/api/account/config")
    def config(request: Request):
        pending = google.get(request.cookies.get(PENDING_COOKIE, ""))
        return {"google": bool(settings.google_client_id and settings.google_client_secret), "domain": domain,
                "google_pending": {"email": pending["email"], "name": pending.get("name", "")} if pending else None}

    @r.get("/api/account/username")
    def check_username(name: str = ""):
        name = name.strip().lower()
        problem = username_problem(name) or taken(name)
        return {"ok": not problem, "problem": problem}

    def taken(name: str) -> str:
        if store.account_by("username", name) or (settings.hosted_dir / name).exists():
            return "That name is taken."
        return ""

    @r.post("/api/account/signup")
    async def signup(request: Request):
        require_csrf(request)
        if signups.blocked():
            raise HTTPException(429, "Too many sign-ups right now. Try again in a while.")
        data = await body(request)
        username = str(data.get("username", "")).strip().lower()
        if problem := username_problem(username) or taken(username):
            raise HTTPException(400, problem)
        call_sign = clean_call_sign(str(data.get("call_sign", "")))
        if not call_sign:
            raise HTTPException(400, "Pick a name for Weave to call you (letters, numbers, spaces).")
        if data.get("terms") is not True:
            raise HTTPException(400, "Please accept the terms and privacy policy.")
        pending = google.get(request.cookies.get(PENDING_COOKIE, ""))
        email, password, sub = None, None, None
        if pending:
            email, sub = pending["email"], pending["sub"]
            if store.account_by("google_sub", sub):
                raise HTTPException(400, "This Google account already has a Weave account. Sign in instead.")
        else:
            email = str(data.get("email", "")).strip().lower()
            password = str(data.get("password", ""))
            if not EMAIL_RE.fullmatch(email):
                raise HTTPException(400, "Enter a valid email address.")
            if len(password) < 8 or len(password) > 200:
                raise HTTPException(400, "Use a password of at least 8 characters.")
        if email and store.account_by("email", email):
            raise HTTPException(400, "That email already has an account. Sign in instead.")
        invite = "".join(ch for ch in str(data.get("invite", "")).upper() if ch.isalnum())[:16] or None
        signups.record()
        try:
            account = await run_in_threadpool(lambda: store.create_account(
                username=username, call_sign=call_sign, email=email, password=password, google_sub=sub,
                referral_code=invite))
        except Exception as e:  # unique constraint: someone took it a moment ago
            log.info("signup conflict: %s", e)
            raise HTTPException(400, "That name or email was just taken. Try another.") from None
        if pending:
            google.get(request.cookies.get(PENDING_COOKIE, ""), pop=True)
        event("site.signup", account=account["id"], google=bool(sub))
        return signed_in(JSONResponse({"ok": True}), request, account["id"])

    @r.post("/api/account/login")
    async def login(request: Request):
        require_csrf(request)
        if logins.blocked():
            raise HTTPException(429, "Too many attempts. Try again in a few minutes.")
        data = await body(request)
        email = str(data.get("email", "")).strip().lower()
        password = str(data.get("password", ""))
        from ..sms.accounts_store import check_password

        account = store.account_by("email", email) if email else None
        ok = bool(account and account["status"] == "active" and account.get("password_hash")
                  and await run_in_threadpool(check_password, password, account["password_hash"]))
        if not ok:
            logins.record()
            raise HTTPException(401, "Wrong email or password.")
        return signed_in(JSONResponse({"ok": True}), request, account["id"])

    @r.post("/api/account/logout")
    def logout(request: Request):
        require_csrf(request)
        token = request.cookies.get(COOKIE, "")
        if token:
            store.delete_account_sessions(raw=token)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @r.get("/auth/google")
    def google_start():
        if not (settings.google_client_id and settings.google_client_secret and base):
            return RedirectResponse("/start?error=google_off", status_code=303)
        state = states.put(True)
        query = urlencode({"client_id": settings.google_client_id, "redirect_uri": f"{base}/auth/google/callback",
                           "response_type": "code", "scope": "openid email profile", "state": state,
                           "prompt": "select_account"})
        return RedirectResponse(f"{GOOGLE_AUTH}?{query}", status_code=303)

    @r.get("/auth/google/callback")
    async def google_callback(request: Request, code: str = "", state: str = ""):
        if not code or not states.get(state, pop=True):
            return RedirectResponse("/start?error=google", status_code=303)
        try:
            info = await run_in_threadpool(google_identity, code)
        except Exception as e:
            log.warning("google sign-in failed: %s", e)
            return RedirectResponse("/start?error=google", status_code=303)
        account = store.account_by("google_sub", info["sub"])
        if account is not None and account["status"] == "active":
            return signed_in(RedirectResponse("/dashboard", status_code=303), request, account["id"])
        if account is not None:
            return RedirectResponse("/start?error=closed", status_code=303)
        key = google.put({"sub": info["sub"], "email": info["email"], "name": info.get("name", "")})
        resp = RedirectResponse("/start?google=1", status_code=303)
        resp.set_cookie(PENDING_COOKIE, key, max_age=900, httponly=True, samesite="lax", secure=secure(request), path="/")
        return resp

    def google_identity(code: str) -> dict:
        import httpx

        with httpx.Client(timeout=15) as client:
            tok = client.post(GOOGLE_TOKEN, data={
                "code": code, "client_id": settings.google_client_id, "client_secret": settings.google_client_secret,
                "redirect_uri": f"{base}/auth/google/callback", "grant_type": "authorization_code"})
            tok.raise_for_status()
            # Google checks the token's signature and expiry for us; we check it was issued to this app.
            info = client.get(GOOGLE_TOKENINFO, params={"id_token": tok.json()["id_token"]})
            info.raise_for_status()
            claims = info.json()
        if claims.get("aud") != settings.google_client_id or claims.get("iss") not in ("accounts.google.com",
                                                                                         "https://accounts.google.com"):
            raise ValueError("token not issued to this app")
        if str(claims.get("email_verified")).lower() != "true" or not claims.get("email"):
            raise ValueError("email not verified")
        return {"sub": str(claims["sub"]), "email": claims["email"].lower(), "name": claims.get("name", "")}

    # ---------- the account ----------
    @r.get("/api/account/me")
    def me(request: Request):
        account = current(request)
        user = store.account_user(account["id"])
        projects = store.account_projects(user["id"]) if user else []
        trash = store.account_projects(user["id"], deleted=True) if user else []
        day = store.usage(user["id"], time.time() - 86_400) if user else {"builds": 0, "tokens_in": 0, "tokens_out": 0}
        month = store.usage(user["id"], time.time() - 30 * 86_400) if user else day
        number = settings.twilio_whatsapp_number or settings.twilio_phone_number
        return {
            "username": account["username"], "call_sign": account["call_sign"], "email": account.get("email"),
            "address": f"https://{account['username']}.{domain}/", "google": bool(account.get("google_sub")),
            "phone": mask(user["phone"]) if user else None, "whatsapp": number,
            "projects": [project_json(user, p) for p in projects],
            "trash": [project_json(user, p) for p in trash],
            "usage": {"today": day, "month": month,
                      "storage_mb": round(dir_size(settings.user_sites_dir / account["username"]) / 1e6, 2)},
            "limits": {"builds_per_day": settings.sms_daily_builds_member + ((user.get("bonus_builds") or 0) if user else 0),
                       "projects": settings.max_projects, "storage_mb": settings.user_storage_mb,
                       "trash_days": settings.trash_days},
            "invite": store.referral_code(user["id"]) if user and settings.sms_referrals else None,
        }

    @r.post("/api/account/phone-code")
    def phone_code(request: Request):
        require_csrf(request)
        account = current(request)
        code = store.create_phone_code(account["id"])
        number = re.sub(r"\D", "", settings.twilio_whatsapp_number or settings.twilio_phone_number or "")
        link = f"https://wa.me/{number}?text={quote('LINK ' + code)}" if number else None
        return {"code": code, "whatsapp_link": link, "expires_in": 3600}

    @r.post("/api/account/call-sign")
    async def call_sign(request: Request):
        require_csrf(request)
        account = current(request)
        name = clean_call_sign(str((await body(request)).get("call_sign", "")))
        if not name:
            raise HTTPException(400, "That name can't be used. Letters, numbers and spaces, up to 30.")
        store.update_account(account["id"], call_sign=name)
        return {"ok": True, "call_sign": name}

    # ---------- projects (the same ones the bot works on) ----------
    @r.post("/api/account/projects")
    async def new_project(request: Request):
        require_csrf(request)
        account = current(request)
        user = store.account_user(account["id"])
        if user is None:
            raise HTTPException(400, "Link your WhatsApp number first.")
        idea = " ".join(str((await body(request)).get("idea", "")).split())[:1500]
        if len(idea) < 10:
            raise HTTPException(400, "Describe it in a sentence or two.")
        # Exactly as if they texted it: same limits, same queue, and the bot replies on WhatsApp.
        msg = Inbound(from_=address(user["phone"], user["channel"]), to=user.get("reply_from") or "",
                      body=f"NEW {idea}", sid=f"site-{secrets.token_hex(12)}")
        outcome = await run_in_threadpool(service.handle, msg)
        return {"ok": outcome in ("queued_new", "ask_style", "ask_kind"), "outcome": outcome}

    @r.post("/api/account/projects/{project_id}/rename")
    async def rename(request: Request, project_id: int):
        require_csrf(request)
        user, project = project_of(current(request), project_id)
        name = " ".join(str((await body(request)).get("name", "")).split())[:60]
        if not name or "<" in name or ">" in name:
            raise HTTPException(400, "Enter a name.")
        store.update_project(project["id"], name=name)
        return {"ok": True}

    @r.post("/api/account/projects/{project_id}/delete")
    def delete(request: Request, project_id: int):
        require_csrf(request)
        user, project = project_of(current(request), project_id)
        service.delete_project(user, project)
        return {"ok": True}

    @r.post("/api/account/projects/{project_id}/restore")
    def restore(request: Request, project_id: int):
        require_csrf(request)
        account = current(request)
        user, project = project_of(account, project_id, deleted=True)
        if len(store.account_projects(user["id"])) >= settings.max_projects:
            raise HTTPException(400, f"You already have {settings.max_projects} projects. Delete one first.")
        if not service.restore_project(user, project):
            raise HTTPException(400, "This project can't be restored any more.")
        return {"ok": True}

    @r.get("/api/account/projects/{project_id}/files")
    def files(request: Request, project_id: int):
        user, project = project_of(current(request), project_id)
        build_id = project.get("last_success_build_id")
        out = {"kind": project.get("kind") or "android", "files": [], "preview": None, "apk": None}
        if not build_id:
            return out
        if out["kind"] == "website":
            site = service.web.site_dir(build_id) if service.web is not None else None
            if site and site.is_dir():
                out["files"] = [{"name": f.name, "bytes": f.stat().st_size} for f in sorted(site.iterdir())
                                if f.is_file() and not f.is_symlink() and PAGE_NAME_RE.fullmatch(f.name)]
            out["preview"] = service.preview_url(user["id"], build_id, relative_ok=True)
        else:
            out["apk"] = service.download_url(user["id"], build_id, relative_ok=True)
        return out

    @r.get("/api/account/projects/{project_id}/files/{name}")
    def file(request: Request, project_id: int, name: str):
        user, project = project_of(current(request), project_id)
        build_id = project.get("last_success_build_id")
        if not build_id or project.get("kind") != "website" or service.web is None or not PAGE_NAME_RE.fullmatch(name):
            raise HTTPException(404)
        root = service.web.site_dir(build_id).resolve()
        f = (root / name).resolve()
        if f.parent != root or not f.is_file():
            raise HTTPException(404)
        # Always a download, never rendered on this origin (it holds the session cookie).
        return FileResponse(f, media_type="application/octet-stream", filename=name,
                            headers={"Content-Security-Policy": PREVIEW_CSP, "Cache-Control": "private, no-store"})

    # ---------- your data ----------
    @r.get("/api/account/export")
    def export(request: Request):
        account = current(request)
        user = store.account_user(account["id"])
        data = {"account": {k: account[k] for k in ("username", "call_sign", "email", "created_at")},
                "phone": user["phone"] if user else None, "projects": [], "messages": []}
        if user:
            for p in store.account_projects(user["id"]) + store.account_projects(user["id"], deleted=True):
                jobs = store.all("SELECT ref, kind, status, prompt, change_request, created_at, finished_at "
                                 "FROM jobs WHERE project_id = ? ORDER BY id", (p["id"],))
                data["projects"].append({**project_json(user, p), "builds": jobs})
            data["messages"] = store.all("SELECT direction, body, created_at FROM messages WHERE user_id = ? "
                                         "ORDER BY id DESC LIMIT 2000", (user["id"],))
        return Response(json.dumps(data, indent=2, default=str), media_type="application/json", headers={
            "Content-Disposition": f'attachment; filename="weave-{account["username"]}.json"'})

    @r.post("/api/account/delete")
    async def delete_account(request: Request):
        require_csrf(request)
        account = current(request)
        if str((await body(request)).get("confirm", "")).strip().lower() != account["username"]:
            raise HTTPException(400, "Type your username to confirm.")
        user = store.account_user(account["id"])
        if user:
            for p in store.account_projects(user["id"]):
                service.delete_project(user, p)
            store.update_user(user["id"], account_id=None, status="deleted",
                              phone=f"deleted:{user['id']}:{user['phone']}")
        # The username stays taken forever, so nobody can pick it up and pose as this person.
        store.update_account(account["id"], status="deleted", email=None, google_sub=None, password_hash=None)
        store.delete_account_sessions(account_id=account["id"])
        event("site.account_deleted", account=account["id"])
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    # ---------- abuse reports (anyone) ----------
    @r.post("/api/report")
    async def report(request: Request):
        require_csrf(request)
        if reports.blocked():
            raise HTTPException(429, "Too many reports right now. Email us instead.")
        data = await body(request)
        site = str(data.get("site", "")).strip()[:300]
        reason = str(data.get("reason", "")).strip().lower()
        details = str(data.get("details", "")).strip()[:2000]
        if domain not in site or reason not in REPORT_REASONS:
            raise HTTPException(400, f"Give the address of a site on {domain} and pick a reason.")
        reports.record()
        rid = store.add_report(site, reason, details)
        event("site.report", logging.WARNING, report=rid, reason=reason)
        for owner in service.owners:
            owner_user = store.get_user(owner)
            if owner_user:
                service._reply(owner, owner_user, f"Abuse report #{rid} ({reason}): {site}\n{details[:300]}\n"
                               "Text TAKEDOWN <username> or TAKEDOWN <username>/<project> to take it offline.")
        return {"ok": True, "id": rid}

    return r


PUBLIC_PREFIXES = ("/start", "/dashboard", "/terms", "/privacy", "/report", "/site/", "/auth/google",
                   "/api/account/", "/api/report")
