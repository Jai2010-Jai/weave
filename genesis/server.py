"""Genesis dashboard + JSON API.

Local browser on 127.0.0.1: no auth (dashboard). Any other client (the phone app over Wi-Fi):
`Authorization: Bearer <access code>` required. Binds to 127.0.0.1 unless started with --lan.
Exceptions reachable from the internet through an HTTPS tunnel: Twilio webhooks (signature-checked) and
/download/<token> links (single-purpose, expiring tokens)."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import auth
from .config import Settings, settings as default_settings
from .core.antigravity import make_ai_client
from .core.jobs import JOB_ID_RE, Orchestrator, QueueFullError, public_job
from .core.schemas import BuildRequest
from .sms.routes import PREVIEW_CSP
from .web.hosting import hosted_file, label_for_host, user_file, user_index_html

log = logging.getLogger("genesis.server")
STATIC = Path(__file__).parent / "static"
MAX_BODY_BYTES = 16_000
LOCAL_HOSTS = {"127.0.0.1", "localhost", "testserver", "::1"}
LOOPBACK_CLIENTS = {"127.0.0.1", "::1", "testclient"}
LOG_TAIL_BYTES = 60_000
# Set by reverse proxies and tunnels (cloudflared, ngrok, Caddy...). A request carrying any of them came from
# outside this machine even though the TCP peer is loopback, so it never gets the local-dashboard bypass.
PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "forwarded", "x-real-ip", "cf-connecting-ip", "true-client-ip")


class RateLimiter:
    """Sliding-window limit on build submissions (each one spends Gemini quota)."""

    def __init__(self, max_events: int, window_s: float):
        self.max, self.window = max_events, window_s
        self.events: deque[float] = deque()
        self.lock = threading.Lock()

    def allow(self) -> bool:
        now = time.monotonic()
        with self.lock:
            while self.events and now - self.events[0] > self.window:
                self.events.popleft()
            if len(self.events) >= self.max:
                return False
            self.events.append(now)
            return True


def _hostname(netloc: str | None) -> str:
    try:
        return urlsplit("//" + (netloc or "")).hostname or ""
    except ValueError:
        return ""


def create_app(settings: Settings | None = None, orchestrator: Orchestrator | None = None,
               sms_provider=None, start_sms_worker: bool | None = None, website_builder=None,
               publisher=None) -> FastAPI:
    s = settings or default_settings
    gemini = make_ai_client(s)
    orch = orchestrator or Orchestrator(s, gemini)
    limiter = RateLimiter(max_events=6, window_s=600)

    sms = None
    if s.sms_provider != "off" or s.whatsapp_provider != "off":
        from .sms.provider import ChannelRouter, make_provider
        from .sms.routes import PUBLIC_PREFIXES, build_router
        from .sms.service import SmsService
        from .sms.store import SmsStore
        from .sms.worker import SmsWorker

        sms_store = SmsStore(s.sms_db_path)
        provider = ChannelRouter(sms_provider or make_provider(s), settings=s)
        from .web.builder import WebsiteBuilder

        sms_service = SmsService(s, sms_store, orch.store)
        # Unavailable (with a reason) until configured. Uses the same Gemini model Genesis auto-selects.
        sms_service.web = website_builder or WebsiteBuilder(
            s, model_resolver=orch.gen.model_name if s.ai_backend == "api" and hasattr(orch.gen, "model_name") else None)
        sms = SmsWorker(s, sms_store, orch, provider, sms_service, publisher=publisher)
        from .sms.voice import VoiceInbox
        sms.voice = VoiceInbox(s)  # tests swap in a fake fetch/transcribe
        if s.chat_brain and hasattr(orch.gen, "ask"):
            from .sms.brain import ChatBrain
            sms_service.brain = ChatBrain(s, orch.gen, website_problem=sms_service.website_problem)
        if hasattr(orch.gen, "ask"):
            # Importing the Gemini SDK takes ~3 s: do it now, not while the first person waits for a reply.
            threading.Thread(target=lambda: __import__("google.genai.types"), name="warm-genai", daemon=True).start()
            from .sms.translate import Translator
            sms_service.translator = Translator(s, orch.gen, sms_store)
        if getattr(s, "nlg_enabled", True):
            from .sms.nlg import NlgPresenter
            nlg_presenter = NlgPresenter(s, orch.gen if hasattr(orch.gen, "_structured") else None)
            sms_service.nlg = nlg_presenter
            sms.nlg = nlg_presenter
        from .site.routes import PUBLIC_PREFIXES as SITE_PREFIXES
        public_prefixes: tuple[str, ...] = PUBLIC_PREFIXES + SITE_PREFIXES
    else:
        public_prefixes = ()
    # The worker also serves the web chat, so it runs whenever messaging isn't switched off.
    run_worker = (sms is not None and s.sms_worker == "embedded") if start_sms_worker is None else start_sms_worker

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if sms is not None and run_worker:
            sms.start()
        try:
            yield
        finally:
            if sms is not None and run_worker:
                sms.stop()

    app = FastAPI(title="Genesis", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    access_code = auth.load_or_create_code(s.data_dir)
    failures = auth.FailureLimiter()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        client_ip = request.client.host if request.client else ""
        host = _hostname(request.headers.get("host"))
        bearer = request.headers.get("authorization", "")
        presented = bearer[7:] if bearer.lower().startswith("bearer ") else ""
        authed = False
        hosted = label_for_host(host, s.sites_domain) if s.website_deploy == "weave" else None
        if hosted:
            # A customer site on <name>.<sites_domain>: public, read-only, sandboxed. Never reaches the API.
            if request.method not in ("GET", "HEAD"):
                return PlainTextResponse("Method not allowed", status_code=405)
            return serve_hosted(hosted, request.url.path)
        if request.method == "GET" and request.url.path == "/get":
            # The phone client APK holds no secrets; anyone on the network may download it.
            return await call_next(request)
        proxied = any(h in request.headers for h in PROXY_HEADERS)
        local = client_ip in LOOPBACK_CLIENTS and host in LOCAL_HOSTS and not proxied
        if sms is not None and request.method == "GET" and request.url.path == "/" and not local:
            return RedirectResponse("/start", status_code=303)  # the public front door is the sign-up page
        if public_prefixes and request.url.path.startswith(public_prefixes):
            # Twilio webhooks verify X-Twilio-Signature, downloads an expiring token, the chat a session cookie.
            # None of them use the access code.
            length = request.headers.get("content-length")
            if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
                return JSONResponse({"detail": "request too large"}, status_code=413)
            request.state.local = local
            resp = await call_next(request)
            resp.headers["X-Content-Type-Options"] = "nosniff"
            resp.headers["Referrer-Policy"] = "no-referrer"
            resp.headers.setdefault("X-Frame-Options", "DENY")
            return resp
        if presented:
            if failures.blocked(client_ip):
                return JSONResponse({"detail": "Too many wrong access codes; try again later"}, status_code=429)
            if not auth.check_code(presented, access_code):
                failures.record(client_ip)
                return JSONResponse({"detail": "Wrong access code"}, status_code=401)
            authed = True
        if not authed and not local:
            # Remote clients must pair first. Also blocks DNS-rebinding (foreign Host header on loopback).
            return JSONResponse({"detail": "Access code required"}, status_code=401)
        origin = request.headers.get("origin")
        if (not authed and request.method != "GET" and origin
                and _hostname(origin.split("://")[-1]) not in LOCAL_HOSTS):
            return JSONResponse({"detail": "cross-origin request rejected"}, status_code=403)
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
            return JSONResponse({"detail": "request too large"}, status_code=413)
        request.state.local = local and not authed
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:"
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    site_headers = {"Cache-Control": "public, max-age=60", "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "no-referrer", "Cross-Origin-Opener-Policy": "same-origin"}
    missing = "This site doesn't exist (yet). Made with Weave."

    def serve_hosted(label: str, path: str):
        """<label>.<sites_domain>: an account's projects (/<project>/...) or, for numbers without an account, one
        site at the root. Public, read-only and sandboxed; it never reaches the API."""
        account = sms.store.account_by("username", label) if sms is not None else None
        if account is None:
            f = hosted_file(s.hosted_dir, label, path)
            if f is None:
                return PlainTextResponse(missing, status_code=404)
            return FileResponse(f, media_type="text/html; charset=utf-8",
                                headers={**site_headers, "Content-Security-Policy": PREVIEW_CSP})
        if account["status"] != "active":  # suspended (taken down) or deleted
            return PlainTextResponse("This site is unavailable.", status_code=404 if account["status"] == "deleted" else 451)
        kind, value = user_file(s.user_sites_dir / label, path)
        if kind == "redirect":
            return RedirectResponse(value, status_code=308)
        if kind == "file":
            return FileResponse(value, media_type="text/html; charset=utf-8",
                                headers={**site_headers, "Content-Security-Policy": PREVIEW_CSP})
        if kind == "index":
            user = sms.store.account_user(account["id"])
            projects = [(p.get("name") or p["slug"], p["slug"]) for p in sms.store.account_projects(user["id"])
                        if p.get("slug") and p.get("deploy_url")] if user else []
            report = f"{s.public_base_url}/report" if s.public_base_url else ""
            return HTMLResponse(user_index_html(label, projects, report), headers={
                **site_headers, "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                                           "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"})
        return PlainTextResponse(missing, status_code=404)

    def get_job(job_id: str) -> dict:
        if not JOB_ID_RE.fullmatch(job_id):
            raise HTTPException(404, "build not found")
        try:
            return orch.store.get(job_id)
        except KeyError:
            raise HTTPException(404, "build not found") from None

    @app.get("/api/health")
    def health():
        doctor = s.doctor()
        model = None
        model_error = None
        if gemini.configured:
            try:
                model = gemini.model_name()
            except Exception as e:  # noqa: BLE001
                model_error = str(e)
        elif hasattr(gemini, "problem"):
            model_error = gemini.problem()
        return {"ready": doctor["ready"] and model is not None, "checks": doctor["checks"],
                "gemini_model": model, "gemini_error": model_error,
                "limits": {"build_timeout_s": s.build_timeout, "max_repairs": s.max_repairs}}

    @app.get("/api/pairing")
    def pairing(request: Request):
        """Connection details for the phone app. Only ever shown to the local dashboard."""
        if not getattr(request.state, "local", False):
            raise HTTPException(403, "pairing details are only available on this computer")
        lan = s.host not in ("127.0.0.1", "localhost", "::1")
        urls = [f"http://{ip}:{s.port}" for ip in auth.lan_addresses()] if lan else []
        qr_svg = None
        if urls:
            import io
            from urllib.parse import quote

            import qrcode
            import qrcode.image.svg

            def svg(data: str) -> str:
                buf = io.BytesIO()
                qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2).save(buf)
                return buf.getvalue().decode()

            qr_svg = svg(f"genesis://pair?u={quote(urls[0], safe='')}&c={auth.normalize_code(access_code)}")
            get_qr_svg = svg(f"{urls[0]}/get")
        return {"lan_enabled": lan, "urls": urls, "access_code": access_code, "qr_svg": qr_svg,
                "get_qr_svg": get_qr_svg if urls else None, "port": s.port,
                "phone_app_available": (s.root / "dist" / "Genesis.apk").is_file()}

    @app.post("/api/builds", status_code=202)
    async def create_build(request: Request):
        try:
            req = BuildRequest.model_validate(await request.json())
        except ValidationError as e:
            msg = "; ".join(err["msg"].removeprefix("Value error, ") for err in e.errors()[:3])
            raise HTTPException(422, msg) from None
        except ValueError:
            raise HTTPException(400, "body must be JSON") from None
        if not limiter.allow():
            raise HTTPException(429, "Too many builds requested; wait a few minutes")
        try:
            job = orch.submit(req)
        except QueueFullError as e:
            raise HTTPException(429, str(e)) from None
        return public_job(job)

    @app.get("/api/builds")
    def list_builds():
        return [public_job(j) for j in orch.store.list(limit=50)]

    @app.get("/api/builds/{job_id}")
    def build_status(job_id: str):
        return public_job(get_job(job_id))

    @app.get("/api/builds/{job_id}/log", response_class=PlainTextResponse)
    def build_log(job_id: str, full: bool = False):
        get_job(job_id)
        p = orch.store.log_path(job_id)
        if not p.is_file():
            return ""
        size = p.stat().st_size
        with open(p, "rb") as f:
            if not full and size > LOG_TAIL_BYTES:
                f.seek(size - LOG_TAIL_BYTES)
            return f.read().decode("utf-8", errors="replace")

    @app.get("/api/builds/{job_id}/apk")
    def download_apk(job_id: str):
        job = get_job(job_id)
        apk = job.get("apk")
        if job.get("status") != "succeeded" or not apk:
            raise HTTPException(409, "No verified APK for this build")
        artifact_dir = orch.store.path(job_id) / "artifact"
        path = (artifact_dir / apk["filename"]).resolve()
        if artifact_dir.resolve() not in path.parents or not path.is_file():
            raise HTTPException(404, "APK file missing")
        return FileResponse(path, media_type="application/vnd.android.package-archive", filename=apk["filename"])

    @app.get("/get")
    def get_phone_app():
        apk = s.root / "dist" / "Genesis.apk"
        if not apk.is_file():
            raise HTTPException(404, "Phone app not built yet: run scripts/build_phone_app.sh")
        return FileResponse(apk, media_type="application/vnd.android.package-archive", filename="Genesis.apk")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    if sms is not None:
        from .sms.chat import build_chat_router

        app.include_router(build_router(s, sms.store, sms.service, sms.provider, orch.store, sms.voice,
                                        websites=sms.service.web))
        app.include_router(build_chat_router(s, sms.store, sms.service, orch.store))
        from .site.routes import build_site_router
        app.include_router(build_site_router(s, sms.store, sms.service, orch.store))

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.state.orchestrator = orch
    app.state.sms = sms
    return app
