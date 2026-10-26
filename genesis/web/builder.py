"""Website builder: runs weave's website generator through genesis/web/bridge.mjs in an isolated child process.

Same shape as the Android Orchestrator (create_job / run_existing / cancel / store), so the SMS worker can run
either. Build records live in data/sites/<id>/ (job.json, build.log, site/), separate from Gradle builds.

Pipeline: generating (Node bridge -> Gemini via weave) -> validating (genesis.web.validate) -> verifying (files
on disk) -> succeeded. A change build rewrites the previous good version's index.html into a NEW record, so a
failed change never touches the last working version."""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from ..config import Settings
from ..core.jobs import JobCancelled, JobStore, Listener, now_iso
from ..core.schemas import BuildRequest
from .validate import WebsiteValidationError, validate_page, validate_site

log = logging.getLogger("genesis.web")

BRIDGE = Path(__file__).with_name("bridge.mjs")
STAGE_PROGRESS = {"queued": 2, "generating": 20, "validating": 80, "verifying": 92, "succeeded": 100, "failed": 100}
MAX_OUTPUT_BYTES = 2_000_000


def page_title(path: Path) -> str:
    """The brand part of a generated page's <title> ("Ember & Oak — Specialty Coffee" -> "Ember & Oak")."""
    import html
    import re

    try:
        m = re.search(r"<title[^>]*>(.*?)</title>", path.read_text(encoding="utf-8")[:20000], re.I | re.S)
    except OSError:
        return ""
    title = html.unescape(m.group(1)).strip() if m else ""
    return re.split(r"\s+[—–|:-]\s+|\s*\|\s*", title, maxsplit=1)[0].strip()


class BridgeError(RuntimeError):
    pass


# (request, env, cwd, timeout_s, cancel_event, log_path) -> result dict
Runner = Callable[[dict, dict, Path, int, threading.Event, Path], dict]


def _limit_resources() -> None:  # runs in the child before exec
    if sys.platform == "win32":
        return
    import resource

    for res, value in ((resource.RLIMIT_CPU, 900), (resource.RLIMIT_FSIZE, 20_000_000)):
        try:
            resource.setrlimit(res, (value, value))
        except (ValueError, OSError):
            pass


def node_runner(settings: Settings) -> Runner:
    def run(request: dict, env: dict, cwd: Path, timeout: int, cancel: threading.Event, log_path: Path) -> dict:
        is_win = sys.platform == "win32"
        popen_kwargs = {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)} if is_win else {"start_new_session": True, "preexec_fn": _limit_resources}
        with open(log_path, "a", encoding="utf-8") as logf:
            proc = subprocess.Popen([settings.node_bin, str(BRIDGE)], cwd=cwd, env=env, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=logf, **popen_kwargs)
            done = threading.Event()
            reason: list[str] = []

            def watchdog() -> None:
                deadline = time.monotonic() + timeout
                while not done.wait(0.5):
                    if cancel.is_set() or time.monotonic() > deadline:
                        reason.append("cancelled" if cancel.is_set() else "timeout")
                        if is_win:
                            try:
                                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                               capture_output=True, timeout=10)
                            except Exception:
                                proc.kill()
                            return
                        else:
                            for sig in (signal.SIGTERM, signal.SIGKILL):
                                try:
                                    os.killpg(proc.pid, sig)
                                except ProcessLookupError:
                                    return
                                if done.wait(3):
                                    return
                            return

            t = threading.Thread(target=watchdog, daemon=True)
            t.start()
            try:
                out, _ = proc.communicate(json.dumps(request).encode())
            finally:
                done.set()
                t.join(5)
        if reason == ["cancelled"]:
            raise JobCancelled()
        if reason == ["timeout"]:
            raise BridgeError(f"Website generation timed out after {timeout}s")
        lines = out[-MAX_OUTPUT_BYTES:].decode("utf-8", errors="replace").strip().splitlines()
        try:
            return json.loads(lines[-1])
        except (IndexError, json.JSONDecodeError):
            raise BridgeError(f"Website engine exited ({proc.returncode}) without a result; see log") from None
    return run


class WebsiteBuilder:
    kind = "website"

    def __init__(self, settings: Settings, runner: Runner | None = None, store: JobStore | None = None,
                 model_resolver: Callable[[], str] | None = None):
        self.s = settings
        self.model_resolver = model_resolver
        self.runner = runner or node_runner(settings)
        self.store = store or JobStore(settings.sites_dir)
        self._slots = threading.BoundedSemaphore(settings.website_workers)
        self._cancel: dict[str, threading.Event] = {}
        self._listeners: dict[str, Listener] = {}
        self.store.mark_interrupted()

    def problem(self) -> str:
        return self.s.website_problem()

    # ---------- public ----------
    def create_job(self, req: BuildRequest, *, base_job_id: str | None = None, change_request: str | None = None,
                   source: str = "sms") -> dict:
        if base_job_id is not None and not self.site_ok(base_job_id):
            raise ValueError("a change build needs a successful base website")
        job = self.store.create(req, base_job_id=base_job_id, change_request=change_request, source=source)
        job.update(kind="website", site=None, research=None)
        self.store.save(job)
        return job

    def run_existing(self, job_id: str, listener: Listener | None = None) -> dict:
        if listener:
            self._listeners[job_id] = listener
        cancel = self._cancel.setdefault(job_id, threading.Event())
        try:
            with self._slots:
                job = self.store.get(job_id)
                job["owner_pid"] = os.getpid()
                try:
                    if cancel.is_set():
                        raise JobCancelled()
                    self._run(job, cancel)
                except JobCancelled:
                    job = self.store.get(job_id)
                    job["cancelled"] = True
                    self._fail(job, "Cancelled by user")
                except Exception as e:  # noqa: BLE001 - every failure is reported
                    log.exception("website build %s crashed", job_id)
                    job = self.store.get(job_id)
                    self._fail(job, f"Internal error: {type(e).__name__}: {e}")
        finally:
            self._cancel.pop(job_id, None)
            self._listeners.pop(job_id, None)
        return self.store.get(job_id)

    def cancel(self, job_id: str) -> bool:
        ev = self._cancel.get(job_id)
        if ev is None:
            return False
        ev.set()
        return True

    def site_dir(self, job_id: str) -> Path:
        return self.store.path(job_id) / "site"

    def site_ok(self, job_id: str) -> bool:
        try:
            job = self.store.get(job_id)
        except KeyError:
            return False
        return job.get("status") == "succeeded" and bool(job.get("site")) and (self.site_dir(job_id) / "index.html").is_file()

    # ---------- pipeline ----------
    def _env(self, sandbox: Path) -> dict:
        """Only what the bridge needs: no Twilio, Netlify or other Genesis secrets."""
        node_dir = str(Path(self.s.node_bin).parent)
        return {"PATH": f"{node_dir}:/usr/bin:/bin", "HOME": str(sandbox), "TMPDIR": str(sandbox),
                "WEAVE_DATA_DIR": str(sandbox / "weave"), "AGY_BIN": "/nonexistent/agy",
                "GEMINI_API_KEY": self.s.gemini_api_key, "GEMINI_MODEL": self._model(),
                "GEMINI_FALLBACK_MODELS": self.s.website_fallback_models, "NODE_OPTIONS": "",
                "GENESIS_AI_PROVIDER": self.s.ai_provider, "GLM_BASE_URL": self.s.glm_base_url,
                "GLM_API_KEY": self.s.glm_api_key if self.s.ai_provider == "glm" else "",
                "GENESIS_WEBSITE_THINKING": self.s.website_thinking,
                # Shared by every build: models overloaded in the last minutes are tried last.
                "GEMINI_HEALTH_FILE": str(self.s.data_dir / "gemini-health.json"),
                # Optional extra pages stop here, leaving time to validate and ship what is done.
                "WEBSITE_DEADLINE_MS": str(int((time.time() + self.s.website_timeout * 0.8) * 1000))}

    def _model(self) -> str:
        if self.s.gemini_model and self.s.gemini_model != "auto":
            return self.s.gemini_model
        if self.model_resolver is not None:
            try:
                return self.model_resolver()
            except Exception as e:  # noqa: BLE001 - the bridge reports the real API error
                log.warning("could not resolve the Gemini model: %s", type(e).__name__)
        return "gemini-flash-latest"

    def _run(self, job: dict, cancel: threading.Event) -> None:
        problem = self.problem()
        if problem:
            self._log(job, f"Website builder unavailable: {problem}")
            return self._fail(job, f"Website builder unavailable: {problem}")
        base_id = job.get("base_job_id")
        d = self.store.path(job["id"])
        site, sandbox = d / "site", d / "sandbox"
        site.mkdir(exist_ok=True)
        sandbox.mkdir(exist_ok=True)
        request = {"engine_dir": Path(self.s.website_engine_dir).expanduser().as_posix(), "out_dir": Path(site).as_posix()}
        if base_id:
            base = self.store.get(base_id)
            request.update(op="modify", base_dir=Path(self.site_dir(base_id)).as_posix(), feedback=job.get("change_request") or job["prompt"],
                           brand_name=base.get("app_name") or "")
            job["app_name"] = base.get("app_name")
            self._stage(job, "generating", "Gemini is applying your change to the website")
        else:
            request.update(op="generate", prompt=job["prompt"], variants=self.s.website_variants,
                           extra_pages=self.s.website_pages)
            self._stage(job, "generating", "Researching a design direction and writing the website")
        self._log(job, f"== Website engine: {request['op']} (model {self._model()}) ==")
        started = time.monotonic()
        engine_done = threading.Event()
        if request["op"] == "generate" and request.get("extra_pages"):
            # The home page is written minutes before the extra pages: tell the listener as soon as it exists so the
            # customer gets a live link right away instead of waiting for every page.
            threading.Thread(target=self._watch_home, args=(job, site, engine_done, cancel), daemon=True,
                             name=f"web-home-{job['id'][:8]}").start()
        try:
            result = self.runner(request, self._env(sandbox), sandbox, self.s.website_timeout, cancel,
                                 self.store.log_path(job["id"]))
        except BridgeError as e:
            return self._fail(job, str(e))
        finally:
            engine_done.set()
            shutil.rmtree(sandbox, ignore_errors=True)
        calls = result.get("calls") or []
        job["ai_calls"] = [{"kind": c.get("phase"), "model": c.get("model"), "ok": c.get("ok"),
                            "seconds": round((c.get("ms") or 0) / 1000, 1), "input_tokens": c.get("input_tokens"),
                            "output_tokens": c.get("output_tokens")} for c in calls]
        self._log(job, f"Engine finished in {time.monotonic() - started:.0f}s; AI calls: "
                       + ", ".join(f"{c.get('phase')}={'ok' if c.get('ok') else 'failed'}" for c in calls))
        if cancel.is_set():
            raise JobCancelled()
        if not result.get("ok"):
            return self._fail(job, f"Website generation failed: {result.get('error') or 'unknown error'}")
        if result.get("research") == "fallback":
            self._log(job, "Note: design research used weave's built-in directions (the research AI call failed).")
        self._stage(job, "validating", "Checking the generated website")
        self._drop_bad_extra_pages(job, site)
        try:
            files = validate_site(site)
        except WebsiteValidationError as e:
            self._log(job, f"!! Generated website rejected: {e}")
            return self._fail(job, f"Generated website failed validation: {e}")
        self._stage(job, "verifying", "Preparing the preview")
        name = (result.get("brand_name") if result.get("research") == "ai" or base_id else "") or \
            page_title(site / "index.html") or result.get("brand_name") or job.get("app_name") or ""
        name = name.strip()[:30] or None
        job.update(app_name=name, research=result.get("research"),
                   site={"files": files, "pages": result.get("pages") or [], "entry": "index.html",
                         "truncated_input": bool(result.get("truncated_input"))})
        if result.get("truncated_input"):
            self._log(job, "Note: weave sends only the first 8,000 characters of the current page to the model.")
        self._log(job, "Files: " + ", ".join(f"{f['file']} ({f['bytes']:,} B)" for f in files))
        job.update(status="succeeded", stage="succeeded", progress=100, message="Website preview ready", error=None)
        self.store.save(job)
        self._log(job, "== SUCCESS: website generated locally (not published) ==")
        self._notify(job, "succeeded")

    def _watch_home(self, job: dict, site: Path, engine_done: threading.Event, cancel: threading.Event) -> None:
        index = site / "index.html"
        while not engine_done.wait(1):
            if cancel.is_set():
                return
            if index.is_file():
                try:
                    validate_page(index.read_text(encoding="utf-8"))
                except (OSError, WebsiteValidationError):
                    return  # the full build reports the problem
                self._log(job, "Home page ready; extra pages still being written")
                self._notify({**job, "home_page": index.as_posix()}, "home_ready")
                return

    def _drop_bad_extra_pages(self, job: dict, site: Path) -> None:
        """An extra page (about.html, ...) that fails the checks is removed rather than failing the whole site;
        links to it go to the home page instead. The home page and design options are never dropped."""
        dropped = []
        for f in sorted(site.glob("*.html")):
            if f.name == "index.html" or f.name.startswith("option"):
                continue
            try:
                validate_page(f.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, WebsiteValidationError) as e:
                self._log(job, f"Dropped extra page {f.name}: {e}")
                f.unlink(missing_ok=True)
                dropped.append(f.name)
        for f in site.glob("*.html") if dropped else []:
            text = f.read_text(encoding="utf-8", errors="replace")
            fixed = text
            for name in dropped:
                fixed = fixed.replace(f'href="{name}"', 'href="index.html"')
            if fixed != text:
                f.write_text(fixed, encoding="utf-8")

    # ---------- helpers ----------
    def _stage(self, job: dict, stage: str, message: str) -> None:
        ev = self._cancel.get(job["id"])
        if ev is not None and ev.is_set():
            raise JobCancelled()
        job.update(status="running", stage=stage, progress=STAGE_PROGRESS[stage], message=message, updated_at=now_iso())
        self.store.save(job)
        self._notify(job, stage)

    def _fail(self, job: dict, error: str) -> None:
        job.update(status="failed", stage="failed", progress=100,
                   message="Build cancelled" if job.get("cancelled") else "Website build failed", error=error[:4000])
        self._log(job, f"== FAILED: {error[:500]} ==")
        self.store.save(job)
        self._notify(job, "failed")

    def _notify(self, job: dict, event: str) -> None:
        listener = self._listeners.get(job["id"])
        if listener is None:
            return
        try:
            listener(dict(job), event)
        except Exception:  # noqa: BLE001
            log.exception("website listener failed for %s", job["id"])

    def _log(self, job: dict, line: str) -> None:
        with open(self.store.log_path(job["id"]), "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {line}\n")


def probe(settings: Settings) -> tuple[bool, str]:
    """Load the weave engine through the bridge without any AI call. Returns (ok, detail)."""
    import tempfile

    engine = Path(settings.website_engine_dir).expanduser()
    with tempfile.TemporaryDirectory(prefix="genesis-web-probe-") as tmp:
        sandbox = Path(tmp)
        env = {"PATH": f"{Path(settings.node_bin).parent}:/usr/bin:/bin", "HOME": str(sandbox),
               "TMPDIR": str(sandbox), "WEAVE_DATA_DIR": str(sandbox / "weave"), "AGY_BIN": "/nonexistent/agy"}
        try:
            result = node_runner(settings)({"op": "probe", "engine_dir": str(engine), "out_dir": str(sandbox)},
                                           env, sandbox, 60, threading.Event(), sandbox / "probe.log")
        except (BridgeError, OSError) as e:
            return False, str(e)
    if not result.get("ok"):
        return False, str(result.get("error"))
    try:
        commit = subprocess.run(["git", "-C", str(engine), "rev-parse", "--short", "HEAD"], capture_output=True,
                                text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = ""
    return True, f"weave modules load (commit {commit or 'unknown'})"
