"""HTTP endpoints for SMS: Twilio webhooks (public, signature-checked), token downloads (public, token-checked)
and local-only admin endpoints for the dashboard machine."""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import parse_qsl

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response

from ..config import Settings
from ..core.jobs import JobStore
from ..web.validate import PAGE_NAME_RE
from .meta import parse_webhook_payload, validate_signature, verify_challenge
from .observe import counters, event
from .phone import mask, normalize_e164
from .provider import SmsProvider
from .service import Inbound, SmsService
from .store import SmsStore
from .voice import VoiceError, VoiceInbox, is_audio

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
MAX_DOWNLOADS_PER_LINK = 25
MAX_PREVIEW_VIEWS_PER_LINK = 200
PREVIEW_PAGE_RE = PAGE_NAME_RE
# Generated websites are untrusted. `sandbox` without allow-same-origin gives the page an opaque origin, so its
# scripts can't read Genesis cookies or call Genesis APIs; the rest limits what it can load or send.
PREVIEW_CSP = ("sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox; default-src 'none'; "
               "script-src 'unsafe-inline' https://cdn.tailwindcss.com https://cdn.jsdelivr.net https://unpkg.com "
               "https://cdnjs.cloudflare.com; style-src 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net "
               "https://cdnjs.cloudflare.com; font-src https://fonts.gstatic.com https://cdn.jsdelivr.net "
               "https://cdnjs.cloudflare.com; img-src https: data:; media-src https:; connect-src 'none'; "
               "form-action 'none'; frame-ancestors 'none'; base-uri 'none'")
# Paths reachable from the internet (through the HTTPS tunnel/proxy) without the access code.
PUBLIC_PREFIXES = ("/api/webhooks/twilio/", "/api/webhooks/whatsapp", "/download/", "/preview/", "/chat", "/api/chat/")


def build_router(settings: Settings, store: SmsStore, service: SmsService, provider: SmsProvider,
                 builds: JobStore, voice: VoiceInbox | None = None, websites=None) -> APIRouter:
    r = APIRouter()
    voice = voice or VoiceInbox(settings)

    def handle_voice(msg: Inbound, media_url: str) -> None:
        try:
            msg.body, msg.heard = voice.to_text(media_url), True
        except VoiceError as e:
            event("sms.voice_failed", logging.WARNING, sid=msg.sid, reason=str(e))
            service.voice_failed(msg, str(e))
            return
        except Exception:  # noqa: BLE001
            logging.getLogger("genesis.sms").exception("voice note %s failed", msg.sid)
            service.voice_failed(msg, "something went wrong while listening")
            return
        event("sms.voice_transcribed", sid=msg.sid, chars=len(msg.body))
        try:
            outcome = service.handle(msg)
            event("sms.inbound_handled", sid=msg.sid, outcome=outcome, voice=True)
        except Exception:  # noqa: BLE001
            logging.getLogger("genesis.sms").exception("inbound handling failed for %s", msg.sid)

    async def verified_form(request: Request) -> dict[str, str]:
        """Parse the form body and check X-Twilio-Signature against the URL Twilio actually called."""
        if not settings.public_base_url:
            event("sms.webhook_misconfigured", logging.ERROR, reason="PUBLIC_BASE_URL unset")
            raise HTTPException(503, "SMS webhook is not configured")
        ctype = request.headers.get("content-type", "")
        if not ctype.startswith("application/x-www-form-urlencoded"):
            event("sms.webhook_rejected", logging.WARNING, reason="content_type")
            raise HTTPException(415, "expected application/x-www-form-urlencoded")
        raw = (await request.body()).decode("utf-8", errors="replace")
        params = dict(parse_qsl(raw, keep_blank_values=True))
        url = settings.public_base_url + request.url.path + (f"?{request.url.query}" if request.url.query else "")
        if not provider.validate_request(url, params, request.headers.get("x-twilio-signature", "")):
            event("sms.webhook_auth_failed", logging.WARNING, path=request.url.path,
                  has_signature=bool(request.headers.get("x-twilio-signature")))
            raise HTTPException(403, "invalid signature")
        if settings.twilio_account_sid and params.get("AccountSid") != settings.twilio_account_sid:
            event("sms.webhook_auth_failed", logging.WARNING, reason="account_sid_mismatch")
            raise HTTPException(403, "wrong account")
        return params

    @r.post("/api/webhooks/twilio/sms")
    async def inbound_sms(request: Request):
        p = await verified_form(request)
        sid = p.get("MessageSid") or p.get("SmsSid") or ""
        if (not re.fullmatch(r"(SM|MM)[0-9a-fA-F]{32}", sid) or not p.get("From")
                or p["From"].strip().lower().startswith("web:")):  # web chat identities never arrive via Twilio
            event("sms.webhook_rejected", logging.WARNING, reason="malformed")
            raise HTTPException(400, "missing MessageSid or From")
        try:
            num_media = int(p.get("NumMedia") or 0)
        except ValueError:
            num_media = 0
        msg = Inbound(from_=p["From"], to=p.get("To", ""), body=p.get("Body", ""), sid=sid,
                      account_sid=p.get("AccountSid", ""), num_media=num_media)
        media_url = p.get("MediaUrl0", "")
        if (settings.voice_enabled and num_media and not msg.body.strip() and is_audio(p.get("MediaContentType0", ""))
                and media_url and await run_in_threadpool(service.voice_allowed, msg.from_)
                and not await run_in_threadpool(store.has_inbound, sid)):
            # Transcribing takes a few seconds; answer Twilio now and reply once we know what was said.
            event("sms.voice_received", sid=sid, phone=mask(p["From"].removeprefix("whatsapp:")))
            voice.submit(lambda: handle_voice(msg, media_url))
            return Response(EMPTY_TWIML, media_type="text/xml; charset=utf-8")
        try:
            outcome = await run_in_threadpool(service.handle, msg)
        except Exception:  # noqa: BLE001 - let Twilio see a 500; MessageSid dedupe makes re-delivery safe
            event("sms.inbound_error", logging.ERROR, sid=sid)
            logging.getLogger("genesis.sms").exception("inbound handling failed for %s", sid)
            raise HTTPException(500, "temporary error") from None
        event("sms.inbound_handled", sid=sid, outcome=outcome)
        # Replies are sent via the REST API from the outbox (retries + delivery callbacks), so TwiML stays empty.
        return Response(EMPTY_TWIML, media_type="text/xml; charset=utf-8")

    @r.post("/api/webhooks/twilio/status")
    async def delivery_status(request: Request):
        p = await verified_form(request)
        sid, status = p.get("MessageSid", ""), p.get("MessageStatus", "")
        code = p.get("ErrorCode") or None
        found = await run_in_threadpool(store.delivery_status, sid, status, code) if sid and status else False
        level = logging.WARNING if status in ("failed", "undelivered") else logging.INFO
        event("sms.delivery_status", level, sid=sid, status=status, code=code, known=found)
        return Response(status_code=204)

    # ---------- Meta WhatsApp Cloud API webhooks (Phase 1) ----------
    @r.get("/api/webhooks/whatsapp")
    async def meta_webhook_verify(request: Request):
        mode = request.query_params.get("hub.mode")
        token = request.query_params.get("hub.verify_token")
        challenge = request.query_params.get("hub.challenge")

        if not settings.meta_verify_token:
            event("meta.webhook_unconfigured", logging.ERROR, reason="META_VERIFY_TOKEN unset")
            raise HTTPException(503, "WhatsApp webhook verification is not configured")

        verified = verify_challenge(mode, token, challenge, settings.meta_verify_token)
        if verified is None:
            event("meta.webhook_verify_failed", logging.WARNING,
                  mode=mode, has_token=bool(token), has_challenge=bool(challenge))
            raise HTTPException(403, "Verification failed")

        event("meta.webhook_verified", logging.INFO)
        return Response(content=verified, media_type="text/plain", status_code=200)

    @r.post("/api/webhooks/whatsapp")
    async def meta_webhook_inbound(request: Request):
        if not settings.meta_app_secret:
            event("meta.webhook_unconfigured", logging.ERROR, reason="META_APP_SECRET unset")
            raise HTTPException(503, "WhatsApp webhook is not configured")

        raw_body = await request.body()
        sig = request.headers.get("x-hub-signature-256")
        if not validate_signature(raw_body, sig, settings.meta_app_secret):
            event("meta.webhook_auth_failed", logging.WARNING, has_signature=bool(sig))
            raise HTTPException(403, "Invalid signature")

        try:
            payload = json.loads(raw_body.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            event("meta.webhook_malformed", logging.WARNING, reason="invalid_json")
            raise HTTPException(400, "Malformed JSON")

        event_data = parse_webhook_payload(payload)

        # 1. Reconcile delivery statuses
        for s in event_data.statuses:
            sid = s.id
            status = s.status
            error_code = str(s.error_code) if s.error_code is not None else (s.error_message or None)
            if sid and status:
                found = await run_in_threadpool(store.delivery_status, sid, status, error_code)
                level = logging.WARNING if status == "failed" else logging.INFO
                event("meta.delivery_status", level, sid=sid, status=status, code=error_code, known=found)

        # 2. Durably enqueue inbound messages in SQLite queue
        for m in event_data.messages:
            sid = m.id
            from_e164 = m.from_
            if not sid or not from_e164:
                event("meta.message_malformed", logging.WARNING, sid=sid)
                continue

            wa_from = f"whatsapp:{from_e164}"
            to_addr = settings.meta_phone_number_id or ""

            if m.type == "text":
                body = m.text or ""
            elif m.type in ("audio", "voice"):
                body = "[voice note]"
            else:
                body = f"[{m.type}]"

            try:
                enqueued = await run_in_threadpool(
                    store.enqueue_inbound,
                    wa_from,
                    body,
                    sid,
                    to_addr,
                    media_id=m.media_id,
                    media_type=m.media_mime_type or m.type,
                )
                if enqueued:
                    event("meta.message_enqueued", sid=sid, type=m.type, has_media=bool(m.media_id))
                else:
                    event("meta.message_duplicate", sid=sid)
            except Exception:
                event("meta.enqueue_error", logging.ERROR, sid=sid)
                logging.getLogger("genesis.sms").exception("Meta inbound enqueue failed for %s", sid)
                raise HTTPException(500, "Persistence failed") from None

        return Response(content="EVENT_RECEIVED", media_type="text/plain", status_code=200)

    @r.get("/download/{token}")
    def download(token: str):
        if not TOKEN_RE.fullmatch(token):
            raise HTTPException(404, "Not found")
        state, row = store.use_download_token(token, MAX_DOWNLOADS_PER_LINK)
        if state == "unknown":
            event("sms.download_denied", logging.WARNING, reason="unknown")
            raise HTTPException(404, "Not found")
        if state != "ok":
            event("sms.download_denied", reason=state)
            return PlainTextResponse("This download link has expired. Text OPEN to Weave for a new one.", status_code=410)
        assert row is not None
        try:
            job = builds.get(row["build_id"])
        except KeyError:
            raise HTTPException(404, "Not found") from None
        apk = job.get("apk")
        if job.get("status") != "succeeded" or not apk:
            raise HTTPException(404, "Not found")
        artifact_dir = builds.path(row["build_id"]) / "artifact"
        path = (artifact_dir / apk["filename"]).resolve()
        if artifact_dir.resolve() not in path.parents or not path.is_file():
            event("sms.download_failed", logging.ERROR, build=row["build_id"], reason="file_missing")
            raise HTTPException(404, "Not found")
        event("sms.download", build=row["build_id"])
        resp = FileResponse(path, media_type="application/vnd.android.package-archive", filename=apk["filename"])
        resp.headers["Cache-Control"] = "private, no-store"
        return resp

    @r.get("/preview/{token}")
    def preview_root(token: str):
        return Response(status_code=308, headers={"Location": f"/preview/{token}/", "Cache-Control": "no-store"})

    @r.get("/preview/{token}/")
    @r.get("/preview/{token}/{page}")
    def preview(token: str, page: str = "index.html"):
        """A generated website, for whoever holds the link the owner of the project was sent."""
        if not TOKEN_RE.fullmatch(token) or not PREVIEW_PAGE_RE.fullmatch(page) or websites is None:
            raise HTTPException(404, "Not found")
        state, row = store.use_download_token(token, MAX_PREVIEW_VIEWS_PER_LINK)
        if state == "unknown":
            event("web.preview_denied", logging.WARNING, reason="unknown")
            raise HTTPException(404, "Not found")
        if state != "ok":
            return PlainTextResponse("This preview link has expired. Ask Weave to \"send the link\" again.", status_code=410)
        assert row is not None
        if not websites.site_ok(row["build_id"]):  # also rejects APK tokens: they name an Android build
            raise HTTPException(404, "Not found")
        site_dir = websites.site_dir(row["build_id"]).resolve()
        path = (site_dir / page).resolve()
        if path.parent != site_dir or not path.is_file():
            raise HTTPException(404, "Not found")
        event("web.preview", build=row["build_id"], page=page)
        return FileResponse(path, media_type="text/html; charset=utf-8", headers={
            "Content-Security-Policy": PREVIEW_CSP, "Cache-Control": "private, no-store",
            "X-Robots-Tag": "noindex, nofollow", "Cross-Origin-Opener-Policy": "same-origin"})

    # ---------- local-only admin (the guard marks loopback dashboard requests as local) ----------
    def require_local(request: Request) -> None:
        if not getattr(request.state, "local", False):
            raise HTTPException(403, "only available on the Weave computer")

    @r.get("/api/sms/status")
    def sms_status(request: Request):
        require_local(request)
        return {"enabled": settings.sms_enabled, "provider": provider.name, "issues": settings.sms_issues(),
                "signup": settings.sms_signup, "worker": settings.sms_worker,
                "webhook_url": f"{settings.public_base_url}/api/webhooks/twilio/sms" if settings.public_base_url else None,
                "owner_numbers": [mask(n) for n in service.owners], "stats": store.stats(), "events": counters(),
                "website_builder": service.website_problem() or "available",
                "website_publishing": service.publish_problem() or "netlify (owner approval per publish)"}

    @r.post("/api/sms/link-codes")
    def link_code(request: Request):
        require_local(request)
        code = store.create_link_code("member")
        chat = f"{settings.public_base_url}/chat#code={code}" if settings.public_base_url else None
        return JSONResponse({"code": code, "expires_in_s": 900, "chat_url": chat,
                             "instructions": f"From the phone, text: LINK {code}"}, headers={"Cache-Control": "no-store"})

    return r
