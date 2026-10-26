"""SMS worker: runs queued SMS builds through the shared Orchestrator and sends the outbox.

Runs embedded in `genesis serve` (default) or as its own process (`python -m genesis sms-worker`).
Jobs are leased in SQLite and kept alive by a heartbeat; if a worker dies, the lease expires and the job is
retried (bounded attempts, exponential backoff) by the next worker loop."""

from __future__ import annotations

import logging
import os
import shutil
import socket
import tempfile
import threading
import time
import uuid
from pathlib import Path

from ..config import Settings
from ..core.jobs import Orchestrator
from ..core.schemas import BuildRequest
from . import messages as M
from .observe import event
from .phone import mask, normalize_e164
from .provider import SendError, SmsProvider
from .service import Inbound, SmsService, address
from .store import SmsStore

JOB_LEASE_S = 120
HEARTBEAT_S = 15
OUTBOX_MAX_ATTEMPTS = 5
# Build errors worth retrying automatically: infrastructure hiccups, not problems with the app itself.
TRANSIENT_MARKERS = ("temporarily unavailable", "interrupted", "internal error", "could not list gemini models")


def is_transient(error: str | None) -> bool:
    e = (error or "").lower()
    return any(m in e for m in TRANSIENT_MARKERS)


class SmsWorker:
    def __init__(self, settings: Settings, store: SmsStore, orchestrator: Orchestrator, provider: SmsProvider,
                 service: SmsService, websites=None, publisher=None, voice=None):
        self.s = settings
        self.store = store
        self.orch = orchestrator
        # Website builder (genesis.web.builder.WebsiteBuilder): same create_job/run_existing/cancel interface.
        self.websites = websites if websites is not None else service.web
        self.publisher = publisher  # genesis.web.deploy.publish; injectable for tests
        self.provider = provider
        self.service = service
        self.voice = voice
        self.nlg = None
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._recover_lock = threading.Lock()  # several job loops share one recovery pass

    def _get_voice(self):
        if self.voice is None:
            from .voice import VoiceInbox
            self.voice = VoiceInbox(self.s)
        return self.voice

    def _get_meta_client(self):
        from .meta import MetaWhatsAppClient
        if hasattr(self.provider, "_meta") and hasattr(self.provider._meta, "_get_client"):
            return self.provider._meta._get_client()
        if hasattr(self.provider, "_get_client"):
            return self.provider._get_client()
        return MetaWhatsAppClient(
            access_token=self.s.meta_access_token,
            phone_number_id=self.s.meta_phone_number_id,
            api_version=self.s.meta_graph_api_version,
        )

    # ---------- lifecycle ----------
    def start(self) -> None:
        # Separate lanes: a slow Android build never holds up websites, and websites run side by side.
        loops = [("sms-inbound", self._inbound_loop, ()), ("sms-outbox", self._outbox_loop, ()),
                 ("sms-housekeeping", self._housekeeping_loop, ())]
        loops += [(f"sms-jobs-android-{i}", self._job_loop, ("android",)) for i in range(self.s.max_concurrent_builds)]
        if self.websites is not None:
            loops += [(f"sms-jobs-web-{i}", self._job_loop, ("website",)) for i in range(self.s.website_workers)]
        for name, target, args in loops:
            t = threading.Thread(target=target, args=args, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        event("sms.worker_started", worker=self.worker_id, provider=self.provider.name)

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout)

    def _job_loop(self, lane: str | None = None) -> None:
        while not self._stop.is_set():
            try:
                if not self.run_once(lane):
                    self._stop.wait(2)
            except Exception:  # noqa: BLE001 - the loop must survive anything
                event("sms.worker_error", logging.ERROR)
                logging.getLogger("genesis.sms").exception("sms job loop error")
                self._stop.wait(5)

    def _outbox_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.send_outbox():
                    self._stop.wait(1)
            except Exception:  # noqa: BLE001
                event("sms.outbox_error", logging.ERROR)
                logging.getLogger("genesis.sms").exception("sms outbox loop error")
                self._stop.wait(5)

    def _inbound_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.process_inbound():
                    self._stop.wait(1)
            except Exception:  # noqa: BLE001
                event("sms.inbound_loop_error", logging.ERROR)
                logging.getLogger("genesis.sms").exception("sms inbound loop error")
                self._stop.wait(3)

    def _housekeeping_loop(self) -> None:
        """Once a day: empty trash older than trash_days and back up the database."""
        self._stop.wait(60)
        while not self._stop.is_set():
            try:
                self.housekeeping()
            except Exception:  # noqa: BLE001
                logging.getLogger("genesis.sms").exception("housekeeping error")
            self._stop.wait(86_400)

    def housekeeping(self) -> dict:
        from ..web.hosting import purge_trash

        purged = purge_trash(self.s.trash_dir, self.s.trash_days)
        backup = self.store.backup(self.s.data_dir / "backups", keep=7)
        event("sms.housekeeping", purged=purged, backup=backup.name if backup else None)
        return {"purged": purged, "backup": backup}

    # ---------- jobs ----------
    def recover(self) -> int:
        """Return jobs whose worker vanished (lease expired) to the queue, or fail them after max attempts."""
        n = 0
        for job in self.store.expired_jobs():
            n += 1
            event("sms.job_recovered", logging.WARNING, ref=job["ref"], attempts=job["attempts"])
            if job["build_id"]:
                self.orch.cancel(job["build_id"])
                if self.websites is not None:
                    self.websites.cancel(job["build_id"])
            self._after_failure(job, "Interrupted: the Weave worker stopped during this build")
        return n

    def run_once(self, lane: str | None = None) -> bool:
        """Recover stale jobs, then run at most one queued job. Returns True if a job was run."""
        if self._recover_lock.acquire(blocking=False):
            try:
                self.recover()
            finally:
                self._recover_lock.release()
        job = self.store.claim_job(self.worker_id, JOB_LEASE_S, lane)
        if job is None:
            return False
        self._run(job)
        return True

    def _engine(self, project: dict | None):
        """The builder for a project: the Android Orchestrator, or the website builder."""
        if project is not None and project.get("kind") == "website":
            return self.websites
        return self.orch

    def _run(self, job: dict) -> None:
        ref = job["ref"]
        user = self.store.user_by_id(job["user_id"])
        project = self.store.project(job["user_id"], job["project_id"])
        event("sms.job_started", ref=ref, attempt=job["attempts"], kind=job["kind"],
              project_kind=(project or {}).get("kind", "android"))
        if job["cancel_requested"]:
            return self._finish_cancelled(job, user)
        if job["kind"] == "publish":
            return self._publish(job, user, project)
        engine = self._engine(project)
        if engine is None:
            self.store.update_job(job["id"], status="failed", error="Website builder unavailable: not set up")
            return self._notify(job, user, f"Sorry, {ref} can't run: website building isn't set up on this Weave server.")
        base_id, change_request = job["base_build_id"], job["change_request"]
        if job["kind"] == "change" and not base_id:
            # Queued while an earlier build of this project ran: apply it to what that build made, or rebuild from
            # the full description (which already includes the change) if it made nothing usable.
            base_id = (project or {}).get("last_success_build_id")
            if base_id and not self.service._build_ok(base_id, (project or {}).get("kind") or "android"):
                base_id = None
            if not base_id:
                change_request = None
        try:
            build = engine.create_job(BuildRequest(prompt=job["prompt"]), base_job_id=base_id,
                                      change_request=change_request, source="sms")
        except (KeyError, ValueError) as e:
            # The base version vanished: rebuild from the full project description instead.
            event("sms.base_missing", logging.WARNING, ref=ref, error=type(e).__name__)
            build = engine.create_job(BuildRequest(prompt=job["prompt"]), source="sms")
        self.store.update_job(job["id"], build_id=build["id"], error=None)

        done = threading.Event()

        def heartbeat() -> None:
            while not done.wait(HEARTBEAT_S):
                current = self.store.heartbeat(job["id"], self.worker_id, JOB_LEASE_S)
                if current and current["cancel_requested"]:
                    engine.cancel(build["id"])

        hb = threading.Thread(target=heartbeat, name=f"sms-heartbeat-{ref}", daemon=True)
        hb.start()
        try:
            result = engine.run_existing(build["id"], listener=lambda b, ev: self._progress(job["id"], b, ev))
        finally:
            done.set()
            hb.join(2)

        calls = result.get("ai_calls") or []
        tokens_in = sum(c.get("input_tokens") or 0 for c in calls if isinstance(c, dict))
        tokens_out = sum(c.get("output_tokens") or 0 for c in calls if isinstance(c, dict))
        if tokens_in or tokens_out:  # per-user Gemini usage (dashboard, cost tracking)
            self.store.update_job(job["id"], tokens_in=job.get("tokens_in", 0) + tokens_in,
                                  tokens_out=job.get("tokens_out", 0) + tokens_out)
        current = self.store.job(job["id"]) or job
        if current["lease_owner"] != self.worker_id or current["status"] != "running":
            # Our lease expired (e.g. the machine slept) and another loop already took this job over.
            event("sms.lease_lost", logging.WARNING, ref=ref, build=build["id"])
            return
        if result.get("status") == "succeeded":
            if engine is self.websites:
                return self._finish_website(current, user, result)
            return self._finish_success(current, user, result)
        if result.get("cancelled") or current["cancel_requested"]:
            return self._finish_cancelled(current, user)
        event("sms.build_failed", logging.WARNING, ref=ref, build=build["id"], transient=is_transient(result.get("error")))
        self._after_failure(current, result.get("error") or "Build failed")

    def _after_failure(self, job: dict, error: str) -> None:
        user = self.store.user_by_id(job["user_id"])
        if job["cancel_requested"]:
            return self._finish_cancelled(job, user)
        if is_transient(error) and job["attempts"] < job["max_attempts"]:
            delay = 30 * 2 ** (job["attempts"] - 1)
            self.store.requeue(job["id"], delay, error)
            event("sms.job_retry", logging.WARNING, ref=job["ref"], attempt=job["attempts"], delay_s=delay)
            fallback = (f"{job['ref']} hit a temporary problem ({M.failure_reason(error)}) "
                        f"I'll retry automatically in about {max(1, delay // 60)} min.")
            if self.nlg:
                facts = {"action": "build_transient_error", "build_ref": job["ref"], "ref": job["ref"], "reason": M.failure_reason(error), "delay_min": max(1, delay // 60)}
                fallback = self.nlg.present("build_transient_error", facts, fallback)
            self._notify(job, user, fallback)
            return
        self.store.update_job(job["id"], status="failed", error=error[:1000])
        event("sms.job_failed", logging.WARNING, ref=job["ref"], attempts=job["attempts"])
        fallback = (f"Sorry, build {job['ref']} failed. {M.failure_reason(error)} "
                    "Reply RETRY to try again, or describe it more simply.")
        if self.nlg:
            facts = {"action": "build_failed", "build_ref": job["ref"], "ref": job["ref"], "reason": M.failure_reason(error)}
            fallback = self.nlg.present("build_failed", facts, fallback)
        self._notify(job, user, fallback)

    def _finish_cancelled(self, job: dict, user: dict | None) -> None:
        self.store.update_job(job["id"], status="cancelled", error="Cancelled by user")
        event("sms.job_cancelled", ref=job["ref"])
        self._notify(job, user, f"Build {job['ref']} cancelled.")

    def _finish_success(self, job: dict, user: dict | None, build: dict) -> None:
        project = self.store.project(job["user_id"], job["project_id"])
        apk = build.get("apk") or {}
        artifact = self.orch.store.path(build["id"]) / "artifact" / apk.get("filename", "")
        if not apk or not artifact.is_file():
            # Never announce success without the file the link will serve.
            return self._after_failure(job, "Build reported success but the APK file is missing")
        if project:
            self.store.update_project(project["id"], name=build.get("app_name") or project["name"],
                                      last_success_build_id=build["id"])
            project = self.store.project(job["user_id"], job["project_id"])
        self.store.update_job(job["id"], status="succeeded", error=None)
        event("sms.job_succeeded", ref=job["ref"], build=build["id"], bytes=apk.get("size"))
        url = self.service.download_url(job["user_id"], build["id"], relative_ok=bool(user and user["channel"] == "web"))
        size = f"{apk.get('size', 0) / 1_048_576:.1f} MB"
        verb = "updated" if job["kind"] == "change" else "ready"
        text = (f"Your {M.name_of(project)} is {verb}!\nBuild: {job['ref']}\nStatus: Successful\n"
                + (f"Download (Android APK, {size}): {url}\n" if url else
                   "Download links are not configured (PUBLIC_BASE_URL). Get the APK from the Weave dashboard.\n")
                + "Open the link on your Android phone to install it. Want changes? Just tell me, like \"add a dark theme\".")
        if self.nlg and url:
            text = self.nlg.present("build_succeeded", {"project_name": M.name_of(project), "build_ref": job["ref"], "url": url}, text)
        self._notify(job, user, text)

    def _finish_website(self, job: dict, user: dict | None, build: dict) -> None:
        if not self.websites.site_ok(build["id"]):
            return self._after_failure(job, "Build reported success but the website files are missing")
        project = self.store.project(job["user_id"], job["project_id"])
        if project:
            # A change keeps the site's name; a new site takes the name the engine found.
            name = project["name"] if job["kind"] == "change" and project["name"] else build.get("app_name") or project["name"]
            self.store.update_project(project["id"], name=name, last_success_build_id=build["id"])
            project = self.store.project(job["user_id"], job["project_id"])
        self.store.update_job(job["id"], status="succeeded", error=None)
        event("sms.job_succeeded", ref=job["ref"], build=build["id"], kind="website")
        verb = "updated" if job["kind"] == "change" else "ready"
        if project and self.s.website_deploy == "weave" and self.s.sites_domain:
            # Weave hosting: every finished version goes live on its own subdomain straight away.
            try:
                live = self._host_on_weave(project, build["id"])
            except (OSError, ValueError, RuntimeError) as e:
                logging.getLogger("genesis.sms").exception("weave hosting failed for %s", job["ref"])
                live = None
                event("sms.host_failed", logging.WARNING, ref=job["ref"], error=type(e).__name__)
            if live and (self.store.job(job["id"]) or {}).get("last_progress_key") == "home_ready":
                return self._notify(job, user, f"All pages of {M.name_of(project)} are up: {live}\n"
                                               "Want changes? Just tell me, like \"make the top darker\".")
            if live:
                text = (f"Your website {M.name_of(project)} is {verb} and live!\nOpen it here: {live}\n"
                        "Want changes? Just tell me, like \"make the top darker\".")
                if self.nlg:
                    text = self.nlg.present("website_succeeded", {"project_name": M.name_of(project),
                                                                  "build_ref": job["ref"], "url": live}, text)
                return self._notify(job, user, text)
        url = self.service.preview_url(job["user_id"], build["id"], relative_ok=bool(user and user["channel"] == "web"))
        lines = [f"Your website {M.name_of(project)} is {verb}!"]
        lines.append(f"Open it here: {url}" if url else "(The link isn't set up on the Weave server yet.)")
        lines.append(f"This link is private, just for you, and works for {M.duration(self.s.sms_link_ttl_hours)}. "
                     "The site is not published on the internet.")
        if project and project.get("deploy_url"):
            lines.append(f"Your public site {project['deploy_url']} still shows the older version.")
        hint = "Want changes? Just tell me, like \"make the top darker\""
        if user and user["role"] == "owner" and not self.service.publish_problem():
            hint += ", or say \"publish it\" to put it online"
        lines.append(hint + ".")
        fallback_web = "\n".join(lines)
        if self.nlg and url:
            fallback_web = self.nlg.present("website_succeeded", {"project_name": M.name_of(project), "build_ref": job["ref"], "url": url}, fallback_web)
        self._notify(job, user, fallback_web)

    def _host_on_weave(self, project: dict, build_id: str, site_dir: Path | None = None) -> str:
        """Publish one website version and record it on the project: https://<username>.<sites_domain>/<slug>/ for
        account holders, https://<name>.<sites_domain>/ for numbers without an account (owners, older users)."""
        from ..web.hosting import (dir_size, project_slug, project_url, publish_hosted, publish_project, site_label,
                                   site_url)
        from ..web.validate import public_problem

        site_dir = site_dir or self.websites.site_dir(build_id)
        if problem := public_problem(site_dir):
            event("sms.host_refused", logging.WARNING, project=project["id"], reason=problem)
            raise ValueError(f"not hosted publicly: {problem}")

        account = self.store.account_for_user(project["user_id"])
        if account is not None:
            user_root = self.s.user_sites_dir / account["username"]
            slug = project.get("slug") or project_slug(
                project.get("name") or "", lambda x: self.store.slug_taken(project["user_id"], x, project["id"])
                or (user_root / x).exists())
            used = dir_size(user_root) - dir_size(user_root / slug) + dir_size(site_dir)
            if used > self.s.user_storage_mb * 1_000_000:
                raise ValueError(f"storage limit of {self.s.user_storage_mb} MB reached")
            publish_project(user_root, site_dir, slug)
            url = project_url(account["username"], slug, self.s.sites_domain)
            self.store.update_project(project["id"], deploy_url=url, deploy_site_id=f"{account['username']}/{slug}",
                                      slug=slug)
            event("sms.hosted", site=f"{account['username']}/{slug}")
            return url
        label = site_label(self.s.hosted_dir, project.get("name") or "", project.get("deploy_site_id"))
        publish_hosted(self.s.hosted_dir, site_dir, label)
        url = site_url(label, self.s.sites_domain)
        self.store.update_project(project["id"], deploy_url=url, deploy_site_id=label)
        event("sms.hosted", site=label)
        return url

    def _publish(self, job: dict, user: dict | None, project: dict | None) -> None:
        """Run an owner-approved publish of one exact website version. Never retried automatically."""
        from ..web.deploy import PublishError, publish

        build_id = job["base_build_id"]
        problem = self.service.publish_problem()
        if project is None or project.get("kind") != "website" or not user or user["role"] != "owner":
            problem = problem or "not an owner-approved website"
        if not problem and (self.websites is None or not self.websites.site_ok(build_id)):
            problem = "the approved version is no longer available"
        if problem:
            self.store.update_job(job["id"], status="failed", error=f"Publish refused: {problem}")
            return self._notify(job, user, f"{job['ref']}: not published ({problem}).")
        if self.s.website_deploy == "weave":
            try:
                url = self._host_on_weave(project, build_id)
            except (OSError, ValueError, RuntimeError) as e:
                self.store.update_job(job["id"], status="failed", error=f"Weave hosting failed: {e}")
                return self._notify(job, user, f"{job['ref']}: publishing failed. Your private preview still works.")
            self.store.update_job(job["id"], status="succeeded", build_id=build_id, error=None)
            return self._notify(job, user, f"Published! {M.name_of(project)} is live at {url}\nBuild: {job['ref']}")
        try:
            out = (self.publisher or publish)(self.s.netlify_auth_token, self.websites.site_dir(build_id),
                                               brand=project.get("name") or "", site_id=project.get("deploy_site_id"))
        except PublishError as e:
            self.store.update_job(job["id"], status="failed", error=f"Netlify publish failed: {e}")
            event("sms.publish_failed", logging.WARNING, ref=job["ref"])
            return self._notify(job, user, f"{job['ref']}: publishing failed ({e}). Your private preview still works.")
        self.store.update_project(project["id"], deploy_url=out["url"], deploy_site_id=out["site_id"])
        self.store.update_job(job["id"], status="succeeded", build_id=build_id, error=None)
        event("sms.published", ref=job["ref"])
        self._notify(job, user, f"Published! {M.name_of(project)} is live at {out['url']}\nBuild: {job['ref']}")

    def _progress(self, job_id: int, build: dict, ev: str) -> None:
        """Build listener: turn real stage changes into rate-limited SMS updates."""
        if ev == "home_ready":
            return self._home_ready(job_id, build)
        table = M.WEB_PROGRESS if build.get("kind") == "website" else M.PROGRESS
        if ev in ("succeeded", "failed") or self.s.sms_progress == "off" or ev not in table:
            return
        key, major, text = table[ev]
        if self.s.sms_progress == "major" and not major:
            return
        job = self.store.job(job_id)
        if job is None or job["cancel_requested"]:
            return
        if build.get("kind") == "website" and build.get("base_job_id") and ev == "generating":
            text = "Applying your change to the website."
        if ev == "building" and build.get("attempts"):
            key = f"building-{len(build['attempts']) + 1}"
            text = f"Rebuilding with the fix (attempt {len(build['attempts']) + 1})."
        now = time.time()
        if key == job["last_progress_key"] or job["last_progress_key"] == "home_ready":
            return  # nothing to add once the home page link is out
        if ev != "repairing" and now - job["last_progress_at"] < self.s.sms_progress_min_interval:
            return
        self.store.update_job(job_id, last_progress_key=key, last_progress_at=now)
        self._notify(job, self.store.user_by_id(job["user_id"]), f"{job['ref']}: {text}")

    def _home_ready(self, job_id: int, build: dict) -> None:
        """A new website's home page exists while its other pages are still being written: put the home page live
        now and send the link, so the customer isn't waiting on pages they haven't asked about yet."""
        from ..web.builder import page_title

        job = self.store.job(job_id)
        if (job is None or job["cancel_requested"] or job["kind"] != "new" or self.s.website_deploy != "weave"
                or not self.s.sites_domain or not build.get("home_page")):
            return
        project = self.store.project(job["user_id"], job["project_id"])
        if project is None:
            return
        home = Path(build["home_page"])
        if not project.get("name"):
            # The site's address comes from its name and never changes, so without a real title the early link waits
            # for the finished build (which knows the brand name).
            if not (title := page_title(home).strip()[:30]):
                return
            self.store.update_project(project["id"], name=title)
            project = self.store.project(job["user_id"], job["project_id"])
        stage = Path(tempfile.mkdtemp(prefix="home-", dir=self.s.data_dir))
        try:
            shutil.copyfile(home, stage / "index.html")
            url = self._host_on_weave(project, build["id"], site_dir=stage)
        except (OSError, ValueError, RuntimeError) as e:
            event("sms.home_host_failed", logging.WARNING, ref=job["ref"], error=type(e).__name__)
            return
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        self.store.update_job(job_id, last_progress_key="home_ready", last_progress_at=time.time())
        event("sms.home_live", ref=job["ref"])
        self._notify(job, self.store.user_by_id(job["user_id"]),
                     f"Your home page is live: {url}\nI'm adding the other pages now and will message you when "
                     "they're up (usually a minute or two).")

    def _notify(self, job: dict, user: dict | None, text: str) -> None:
        if user is None or user["status"] != "active":
            return
        limit = M.MAX_CHARS if user["channel"] == "sms" else M.MAX_CHARS_CHAT
        self.service.queue_text(address(user["phone"], user["channel"]), user["id"], text, limit, job["id"],
                                user["reply_from"] if user["channel"] != "web" else None)

    # ---------- outbox ----------
    def send_outbox(self) -> int:
        msgs = self.store.claim_outbound()
        callback = (f"{self.s.public_base_url}/api/webhooks/twilio/status"
                    if self.s.public_base_url.startswith("https://") and self.provider.name == "twilio" else None)
        for m in msgs:
            user = self.store.user_by_id(m["user_id"]) if m["user_id"] else None
            if user is not None and user["status"] == "opted_out":
                self.store.outbound_failed(m["id"], "recipient opted out", None)
                continue
            try:
                res = self.provider.send(m["phone"], m["body"], callback if not m["phone"].startswith("web:") else None,
                                         m["sender"])
            except SendError as e:
                retry = None if e.permanent or m["attempts"] >= OUTBOX_MAX_ATTEMPTS else 10 * 2 ** m["attempts"]
                self.store.outbound_failed(m["id"], str(e), retry)
                event("sms.send_failed", logging.WARNING if retry else logging.ERROR, msg=m["id"],
                      phone=mask(m["phone"]), code=e.code, permanent=e.permanent, retry_in=retry)
                continue
            self.store.outbound_sent(m["id"], res.sid, res.status)
            event("sms.sent", msg=m["id"], sid=res.sid, phone=mask(m["phone"]), chars=len(m["body"]))
        return len(msgs)

    # ---------- inbound queue ----------
    def process_inbound(self) -> int:
        from .meta import MetaAPIError, MetaAuthError, MetaNetworkError
        from .voice import VoiceError

        msgs = self.store.claim_inbound()
        for m in msgs:
            phone = m["phone"]
            sid = m["provider_sid"]
            media_id = m.get("media_id")
            media_type = m.get("media_type")
            is_voice = bool(media_id) or (m["body"] == "[voice note]" and phone.startswith("whatsapp:"))

            try:
                if is_voice and media_id:
                    # Inbound WhatsApp voice note with Meta media_id
                    if not self.service.voice_allowed(phone):
                        # Unknown/unlinked sender: let handle prompt them with link code without transcribing
                        msg = Inbound(from_=phone, to=m["sender"] or "", body="[voice note]", sid=sid, num_media=1)
                        outcome = self.service.handle(msg)
                        user = self.store.get_user(normalize_e164(phone, self.s.sms_default_country_code) or "")
                        self.store.inbound_done(m["id"], user["id"] if user else None)
                        event("sms.inbound_processed", msg=m["id"], sid=sid, outcome=outcome)
                        continue

                    if not self.s.voice_enabled:
                        user = self.store.get_user(normalize_e164(phone, self.s.sms_default_country_code) or "")
                        self.service._reply(phone, user, "Voice notes are currently disabled on this server. Please send a text message.")
                        self.store.inbound_done(m["id"], user["id"] if user else None)
                        continue

                    # Fetch & transcribe
                    voice = self._get_voice()
                    meta_client = self._get_meta_client()
                    audio_bytes, _ = voice.fetch_meta(media_id, meta_client)
                    transcript = voice.transcribe(audio_bytes).strip()
                    if not transcript:
                        raise VoiceError("I couldn't hear any words in that voice note")

                    # Save transcribed body to store for history & audit
                    self.store.update_inbound_body(m["id"], transcript)

                    msg = Inbound(
                        from_=phone,
                        to=m["sender"] or "",
                        body=transcript,
                        sid=sid,
                        heard=True,
                        num_media=1,
                    )
                    outcome = self.service.handle(msg)
                    user = self.store.get_user(normalize_e164(phone, self.s.sms_default_country_code) or "")
                    self.store.inbound_done(m["id"], user["id"] if user else None)
                    event("sms.voice_processed", msg=m["id"], sid=sid, chars=len(transcript), outcome=outcome)
                else:
                    is_media = m["body"].startswith("[") and m["body"].endswith("]")
                    msg = Inbound(
                        from_=phone,
                        to=m["sender"] or "",
                        body=m["body"],
                        sid=sid,
                        num_media=1 if is_media else 0,
                    )
                    outcome = self.service.handle(msg)
                    user = self.store.get_user(normalize_e164(phone, self.s.sms_default_country_code) or "")
                    self.store.inbound_done(m["id"], user["id"] if user else None)
                    event("sms.inbound_processed", msg=m["id"], sid=sid, outcome=outcome)

            except VoiceError as e:
                # Local voice errors (audio too long, unhearable, unsupported type, etc.)
                event("sms.voice_failed", logging.WARNING, sid=sid, reason=str(e))
                self.service.voice_failed(Inbound(from_=phone, to=m["sender"] or "", body="[voice note]", sid=sid), str(e))
                self.store.inbound_failed(m["id"], str(e), retry_in=None)
            except MetaAuthError as e:
                # Permanent auth failure: notify user and do not retry endlessly
                event("sms.voice_failed", logging.ERROR, sid=sid, reason="auth_error")
                self.service.voice_failed(Inbound(from_=phone, to=m["sender"] or "", body="[voice note]", sid=sid), "WhatsApp authorization failed")
                self.store.inbound_failed(m["id"], str(e), retry_in=None)
            except (MetaAPIError, MetaNetworkError) as e:
                permanent = getattr(e, "permanent", False) or m["attempts"] >= 3
                event("sms.voice_failed", logging.WARNING, sid=sid, error=str(e), permanent=permanent)
                if permanent:
                    self.service.voice_failed(Inbound(from_=phone, to=m["sender"] or "", body="[voice note]", sid=sid), "could not download voice note")
                    self.store.inbound_failed(m["id"], str(e), retry_in=None)
                else:
                    retry_in = 10 * 2 ** (m["attempts"] - 1)
                    self.store.inbound_failed(m["id"], str(e), retry_in=retry_in)
            except Exception as e:  # noqa: BLE001
                permanent = m["attempts"] >= 3
                event("sms.inbound_failed", logging.WARNING if not permanent else logging.ERROR,
                      msg=m["id"], sid=sid, error=str(e), attempts=m["attempts"])
                if permanent:
                    if is_voice:
                        self.service.voice_failed(Inbound(from_=phone, to=m["sender"] or "", body="[voice note]", sid=sid), "processing failed")
                    self.store.inbound_failed(m["id"], str(e), retry_in=None)
                else:
                    retry_in = 10 * 2 ** (m["attempts"] - 1)
                    self.store.inbound_failed(m["id"], str(e), retry_in=retry_in)
        return len(msgs)

    def drain(self, max_jobs: int = 10) -> None:
        """Run queued jobs and send all pending messages synchronously (CLI simulation and tests)."""
        self.process_inbound()
        self.send_outbox()
        for _ in range(max_jobs):
            if not self.run_once():
                break
            self.process_inbound()
            self.send_outbox()
        self.process_inbound()
        self.send_outbox()
