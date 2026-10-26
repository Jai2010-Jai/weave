"""SMS channel: Twilio webhook security, identity, conversation, queue, worker, delivery and downloads.
Twilio and Gemini are faked; the real Orchestrator, validator, JobStore and workspace handling are used."""

import threading
import time
import uuid
from urllib.parse import urlencode

import pytest
from conftest import FakeBuilder, FakeGemini, manifest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from genesis.core.gemini import GeminiError
from genesis.core.jobs import Orchestrator
from genesis.server import create_app
from genesis.sms import worker as worker_mod
from genesis.sms.phone import mask, normalize_e164
from genesis.sms.provider import SendError, SendResult
from genesis.sms.service import compose_spec

OWNER = "+919876500001"
STRANGER = "+919876500099"
WEBHOOK = "/api/webhooks/twilio/sms"


class FakeProvider:
    name = "twilio"

    def __init__(self, settings):
        self.validator = RequestValidator(settings.twilio_auth_token)
        self.sent: list[tuple[str, str]] = []
        self.failures: list[SendError] = []
        self.senders: list[str | None] = []

    def send(self, to, body, status_callback, sender=None):
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append((to, body))
        self.senders.append(sender)
        return SendResult(sid="SM" + uuid.uuid4().hex, status="queued")

    def validate_request(self, url, params, signature):
        return bool(signature) and self.validator.validate(url, params, signature)

    def bodies(self, to=None):
        return [b for t, b in self.sent if to is None or t == to]


class Env:
    def __init__(self, settings, gemini=None, builder=None):
        self.settings = settings
        self.gemini = gemini or FakeGemini(manifest())
        self.builder = builder or FakeBuilder(settings)
        self.orch = Orchestrator(settings, self.gemini, builder=self.builder)
        self.provider = FakeProvider(settings)
        self.app = create_app(settings, orchestrator=self.orch, sms_provider=self.provider, start_sms_worker=False)
        self.client = TestClient(self.app)
        self.sms = self.app.state.sms
        self.store = self.sms.store

    def sign(self, path, params):
        return RequestValidator(self.settings.twilio_auth_token).compute_signature(
            self.settings.public_base_url + path, params)

    def text(self, frm, body, sid=None, signature=None, extra=None):
        params = {"MessageSid": sid or "SM" + uuid.uuid4().hex, "AccountSid": self.settings.twilio_account_sid,
                  "From": frm, "To": self.settings.twilio_phone_number, "Body": body, "NumMedia": "0"}
        params.update(extra or {})
        sig = self.sign(WEBHOOK, params) if signature is None else signature
        return self.client.post(WEBHOOK, content=urlencode(params),
                                headers={"content-type": "application/x-www-form-urlencoded", "x-twilio-signature": sig})

    def drain(self):
        self.sms.drain()

    def last(self, to=OWNER):
        self.sms.send_outbox()
        bodies = self.provider.bodies(to)
        return bodies[-1] if bodies else None

    def user(self, phone=OWNER):
        return self.store.get_user(phone)

    def jobs(self, phone=OWNER):
        u = self.user(phone)
        return self.store.all("SELECT * FROM jobs WHERE user_id = ? ORDER BY id", (u["id"],)) if u else []


@pytest.fixture
def env(settings):
    return Env(settings)


def link_from(text):
    return next(w for w in text.split() if "/download/" in w)


# ---------- webhook security & parsing ----------
def test_valid_signature_accepted_and_returns_twiml(env):
    r = env.text(OWNER, "HELP")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/xml")
    assert "<Response></Response>" in r.text
    assert "new app: <idea>" in env.last()


@pytest.mark.parametrize("sig", ["", "bm90LWEtcmVhbC1zaWduYXR1cmU="])
def test_invalid_or_missing_signature_rejected(env, sig):
    r = env.text(OWNER, "Build me a habit tracker", signature=sig)
    assert r.status_code == 403
    assert env.store.all("SELECT * FROM messages") == []


def test_signature_over_tampered_body_rejected(env):
    params = {"MessageSid": "SM" + "a" * 32, "AccountSid": env.settings.twilio_account_sid, "From": OWNER, "Body": "hi"}
    sig = env.sign(WEBHOOK, params)
    params["Body"] = "Build me something else"
    r = env.client.post(WEBHOOK, content=urlencode(params),
                        headers={"content-type": "application/x-www-form-urlencoded", "x-twilio-signature": sig})
    assert r.status_code == 403


def test_signature_must_match_public_url(env):
    params = {"MessageSid": "SM" + "b" * 32, "AccountSid": env.settings.twilio_account_sid, "From": OWNER, "Body": "HELP"}
    sig = RequestValidator(env.settings.twilio_auth_token).compute_signature("http://localhost:8765" + WEBHOOK, params)
    assert env.text(OWNER, "HELP", sid=params["MessageSid"], signature=sig).status_code == 403


def test_wrong_account_and_malformed_requests(env):
    assert env.text(OWNER, "HELP", extra={"AccountSid": "AC" + "1" * 32}).status_code == 403
    assert env.text(OWNER, "HELP", sid="not-a-sid").status_code == 400
    r = env.client.post(WEBHOOK, json={"From": OWNER}, headers={"x-twilio-signature": "x"})
    assert r.status_code == 415


def test_webhook_unavailable_without_public_url(env):
    env.settings.public_base_url = ""
    assert env.text(OWNER, "HELP").status_code == 503


def test_webhook_reachable_through_tunnel_but_dashboard_api_is_not(env):
    tunnel = {"host": "genesis.example.test", "x-forwarded-for": "203.0.113.9"}
    params = {"MessageSid": "SM" + "c" * 32, "AccountSid": env.settings.twilio_account_sid, "From": OWNER, "Body": "HELP"}
    r = env.client.post(WEBHOOK, content=urlencode(params), headers={
        **tunnel, "content-type": "application/x-www-form-urlencoded", "x-twilio-signature": env.sign(WEBHOOK, params)})
    assert r.status_code == 200
    assert env.client.get("/api/builds", headers=tunnel).status_code == 401
    # Even with a local Host header, a proxied request is not treated as the local dashboard.
    assert env.client.get("/api/builds", headers={"x-forwarded-for": "203.0.113.9"}).status_code == 401
    assert env.client.get("/api/sms/status", headers={"x-forwarded-for": "203.0.113.9"}).status_code == 401


def test_duplicate_delivery_is_idempotent(env):
    sid = "SM" + "d" * 32
    assert env.text(OWNER, "Build me a habit tracker with streaks", sid=sid).status_code == 200
    assert env.text(OWNER, "Build me a habit tracker with streaks", sid=sid).status_code == 200
    assert len(env.jobs()) == 1
    env.sms.send_outbox()
    assert sum("Build ID" in b for b in env.provider.bodies()) == 1


def test_media_only_message(env):
    env.text(OWNER, "", extra={"NumMedia": "1"})
    assert "only read text" in env.last()


# ---------- identity, onboarding, linking ----------
def test_unknown_sender_gets_one_instruction_reply(env):
    env.text(STRANGER, "Build me a habit tracker")
    env.text(STRANGER, "Hello?")
    env.sms.send_outbox()
    replies = env.provider.bodies(STRANGER)
    assert len(replies) == 1 and "LINK <code>" in replies[0]
    assert env.user(STRANGER) is None and env.jobs(STRANGER) == []


def test_closed_signup_rejects_link(env):
    env.settings.sms_signup = "closed"
    code = env.store.create_link_code()
    env.text(STRANGER, f"LINK {code}")
    assert env.user(STRANGER) is None
    assert "isn't authorized" in env.last(STRANGER)


def test_link_code_flow_single_use_and_lockout(env):
    code = env.store.create_link_code()
    env.text(STRANGER, f"LINK {code.lower()}")
    assert env.user(STRANGER)["role"] == "member"
    assert "linked" in env.last(STRANGER)
    other = "+919876500055"
    env.text(other, f"LINK {code}")
    assert env.user(other) is None  # codes are single-use
    for _ in range(5):
        env.text(other, "LINK WRONGCODE1")
    good = env.store.create_link_code()
    env.text(other, f"LINK {good}")
    assert env.user(other) is None  # locked out after 5 failures


def test_expired_link_code_rejected(env):
    code = env.store.create_link_code(ttl_s=-1)
    env.text(STRANGER, f"LINK {code}")
    assert env.user(STRANGER) is None


def test_link_code_endpoint_is_local_only(env):
    r = env.client.post("/api/sms/link-codes")
    assert r.status_code == 200 and len(r.json()["code"]) == 10
    remote = TestClient(env.app, client=("192.168.1.50", 5000), base_url="http://192.168.1.10:8765")
    assert remote.post("/api/sms/link-codes").status_code == 401


def test_open_signup_creates_isolated_guest(env):
    env.settings.sms_signup = "open"
    env.text(STRANGER, "Build me a flashcards app for Spanish verbs")
    assert env.user(STRANGER)["role"] == "guest"
    assert len(env.jobs(STRANGER)) == 1


def test_supplying_another_users_ids_grants_nothing(env):
    env.text(OWNER, "Build me a habit tracker with streaks")
    env.drain()
    owner_ref = env.jobs()[0]["ref"]
    code = env.store.create_link_code()
    env.text(STRANGER, f"LINK {code}")
    env.text(STRANGER, f"STATUS {owner_ref}")
    assert owner_ref not in env.last(STRANGER) and "No builds yet" in env.last(STRANGER)
    env.text(STRANGER, "USE 1")
    assert "couldn't find" in env.last(STRANGER)
    env.text(STRANGER, "OPEN 1")
    assert "/download/" not in env.last(STRANGER)
    env.text(STRANGER, "PROJECTS")
    assert "No projects yet" in env.last(STRANGER)


def test_opt_out_and_back_in(env):
    env.text(OWNER, "STOP")
    assert env.user()["status"] == "opted_out"
    env.sms.send_outbox()
    assert env.provider.bodies() == []
    env.text(OWNER, "Build me a habit tracker")
    assert env.jobs() == []
    env.text(OWNER, "START")
    assert env.user()["status"] == "active" and "Welcome back" in env.last()


# ---------- end-to-end conversation ----------
def test_sms_to_apk_download_flow(env):
    env.text(OWNER, "Build me a calorie tracker with a pink UI")
    ack = env.last()
    assert "Build ID: G-" in ack and "I'll message you" in ack
    job = env.jobs()[0]
    assert job["status"] == "queued"

    env.drain()
    job = env.store.job(job["id"])
    assert job["status"] == "succeeded"
    build = env.orch.store.get(job["build_id"])
    assert build["status"] == "succeeded" and build["source"] == "sms"
    bodies = env.provider.bodies()
    assert any("writing the app's code" in b for b in bodies)
    assert any("Compiling" in b for b in bodies)
    final = bodies[-1]
    assert "Lift Log is ready" in final and job["ref"] in final and "Status: Successful" in final
    assert len(final) <= 450

    url = link_from(final)
    assert url.startswith("https://genesis.example.test/download/")
    r = env.client.get(url.removeprefix("https://genesis.example.test"), headers={"host": "genesis.example.test",
                                                                                  "x-forwarded-for": "198.51.100.7"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/vnd.android.package-archive"
    artifact = env.orch.store.path(build["id"]) / "artifact" / build["apk"]["filename"]
    assert r.content == artifact.read_bytes()
    assert "workspace" not in final and "data/builds" not in final
    # The dashboard shows the SMS build (with its logs) like any other.
    assert [b["id"] for b in env.client.get("/api/builds").json()] == [build["id"]]


def test_follow_up_modifies_existing_project(env):
    env.text(OWNER, "Build me a habit tracker")
    env.drain()
    first = env.jobs()[0]
    env.text(OWNER, "Make the theme dark and add reminders")
    assert "Updating Lift Log" in env.last()
    env.drain()
    jobs = env.jobs()
    assert len(jobs) == 2 and jobs[1]["kind"] == "change" and jobs[1]["project_id"] == first["project_id"]
    assert jobs[1]["base_build_id"] == first["build_id"]
    change, files, base = env.gemini.modify_calls[0]
    assert change == "Make the theme dark and add reminders" and "GenesisApp.kt" in files
    build = env.orch.store.get(jobs[1]["build_id"])
    assert build["status"] == "succeeded" and build["base_job_id"] == first["build_id"]
    assert build["package_id"] == "com.genesis.liftlog"  # same app id, installs as an update
    ws = env.orch.store.path(build["id"]) / "workspace"
    assert '"dark"' in (ws / "app/src/main/java/com/genesis/app/ui/theme/ThemeConfig.kt").read_text()
    assert "is updated" in env.last()
    project = env.store.project(env.user()["id"], first["project_id"])
    assert project["last_success_build_id"] == build["id"]
    assert env.store.conversation(env.user()["id"])[0]["body"] == "Build me a habit tracker"


def test_change_after_failed_first_build_rebuilds_from_full_spec(settings):
    env = Env(settings, gemini=FakeGemini(manifest(), errors=[GeminiError("Gemini request failed: bad")]))
    env.text(OWNER, "Build me a habit tracker")
    env.drain()
    assert env.jobs()[0]["status"] == "failed"
    env.text(OWNER, "CHANGE add a dark theme")
    env.drain()
    job = env.jobs()[1]
    assert job["kind"] == "new" and job["status"] == "succeeded"
    assert "Build me a habit tracker" in env.gemini.prompts[-1] and "add a dark theme" in env.gemini.prompts[-1]


def test_new_intent_starts_separate_project(env):
    env.text(OWNER, "Build me a habit tracker")
    env.drain()
    env.text(OWNER, "Create a new app for splitting bills with friends")
    jobs = env.jobs()
    assert jobs[1]["kind"] == "new" and jobs[1]["project_id"] != jobs[0]["project_id"]
    env.drain()
    env.text(OWNER, "NEW")
    assert "What should it do" in env.last()
    env.text(OWNER, "A flashcards app for chemistry")
    assert env.jobs()[2]["kind"] == "new"


def test_ambiguous_message_with_no_active_project_asks(env):
    env.text(OWNER, "Build me a habit tracker")
    env.drain()
    env.text(OWNER, "NEW Build me a workout log app")
    env.drain()
    env.store.update_user(env.user()["id"], active_project_id=None)
    env.text(OWNER, "add reminders")
    reply = env.last()
    assert "Which app" in reply and "USE <n>" in reply and "1 Lift Log" in reply
    assert len(env.jobs()) == 2
    env.text(OWNER, "USE 1")
    assert "Now working on" in env.last()
    env.text(OWNER, "add reminders")
    assert env.jobs()[2]["project_id"] == env.jobs()[0]["project_id"]


def test_commands(env):
    env.text(OWNER, "STATUS")
    assert "No builds yet" in env.last()
    env.text(OWNER, "Build me a habit tracker")
    env.text(OWNER, "STATUS")
    assert "waiting for a build slot" in env.last()
    env.drain()
    env.text(OWNER, "STATUS")
    assert "is ready" in env.last()
    env.text(OWNER, "PROJECTS")
    assert "1 Lift Log - ready *" in env.last()
    env.text(OWNER, "OPEN")
    assert "/download/" in env.last() and "Android APK" in env.last()
    env.text(OWNER, "RETRY")
    assert "nothing to retry" in env.last()
    env.text(OWNER, "LINK ABC")
    assert "already linked" in env.last()


def test_new_project_link_requires_success(env):
    env.text(OWNER, "Build me a habit tracker")
    env.text(OWNER, "OPEN")
    assert "no finished build" in env.last()


# ---------- limits ----------
def test_one_build_at_a_time_per_user(env):
    env.text(OWNER, "Build me a habit tracker")
    env.text(OWNER, "Build me a different app for recipes")
    assert "still in progress" in env.last()
    assert len(env.jobs()) == 1


def test_daily_quota(env):
    env.settings.sms_daily_builds_member = 1
    env.text(OWNER, "Build me a habit tracker")
    env.drain()
    env.text(OWNER, "add a dark theme please")
    assert "limit of 1 builds per day" in env.last()
    assert len(env.jobs()) == 1


def test_global_queue_limit(env):
    env.settings.sms_max_queued = 1
    env.text(OWNER, "Build me a habit tracker")
    code = env.store.create_link_code()
    env.text(STRANGER, f"LINK {code}")
    env.text(STRANGER, "Build me a flashcards app")
    assert "busy with other builds" in env.last(STRANGER)


def test_inbound_rate_limit(env):
    env.settings.sms_inbound_per_hour = 5
    for i in range(8):
        env.text(OWNER, f"HELP {i}")
    env.sms.send_outbox()
    bodies = env.provider.bodies()
    assert sum("too quickly" in b for b in bodies) == 1
    assert sum("Just talk to me" in b for b in bodies) == 5


def test_progress_updates_are_rate_limited(env):
    env.settings.sms_progress_min_interval = 3600
    env.text(OWNER, "Build me a habit tracker")
    env.drain()
    bodies = env.provider.bodies()
    assert len(bodies) == 2 and "Build ID" in bodies[0] and "is ready" in bodies[1]


def test_progress_off(env):
    env.settings.sms_progress = "off"
    env.text(OWNER, "Build me a habit tracker")
    env.drain()
    assert len(env.provider.bodies()) == 2


def test_repair_progress_is_reported_truthfully(settings):
    from conftest import BROKEN, GOOD

    env = Env(settings, gemini=FakeGemini(manifest(BROKEN), repairs=[GOOD]))
    env.text(OWNER, "Build me a habit tracker")
    env.drain()
    bodies = env.provider.bodies()
    assert any("Found a build error" in b for b in bodies)
    assert any("attempt 2" in b for b in bodies)
    assert "is ready" in bodies[-1]


# ---------- failures, retries, recovery, cancellation ----------
def test_permanent_build_failure_notifies_once(settings):
    from conftest import BROKEN

    env = Env(settings, gemini=FakeGemini(manifest(BROKEN), repairs=[BROKEN, BROKEN]))
    env.text(OWNER, "Build me a habit tracker")
    env.drain()
    job = env.jobs()[0]
    assert job["status"] == "failed" and job["attempts"] == 1
    final = env.last()
    assert "failed" in final and "didn't compile" in final and job["ref"] in final and "RETRY" in final
    env.text(OWNER, "RETRY")
    assert "Retrying" in env.last() and len(env.jobs()) == 2


def test_transient_failure_retries_with_backoff(settings):
    env = Env(settings, gemini=FakeGemini(manifest(), errors=[GeminiError("Gemini is temporarily unavailable, please retry: 503")]))
    env.text(OWNER, "Build me a habit tracker")
    env.drain()
    job = env.jobs()[0]
    assert job["status"] == "queued" and job["attempts"] == 1 and job["next_run_at"] > time.time() + 20
    assert "retry automatically" in env.last()
    env.store.update_job(job["id"], next_run_at=0)
    env.drain()
    job = env.store.job(job["id"])
    assert job["status"] == "succeeded" and job["attempts"] == 2


def test_transient_failures_are_bounded(settings):
    errs = [GeminiError("Gemini is temporarily unavailable") for _ in range(5)]
    env = Env(settings, gemini=FakeGemini(manifest(), errors=errs))
    env.settings.sms_job_max_attempts = 2
    env.text(OWNER, "Build me a habit tracker")
    for _ in range(3):
        env.drain()
        env.store.update_job(env.jobs()[0]["id"], next_run_at=0)
    job = env.jobs()[0]
    assert job["status"] == "failed" and job["attempts"] == 2
    assert "AI service is temporarily unavailable" in env.last()


def test_worker_crash_recovery(env):
    env.text(OWNER, "Build me a habit tracker")
    job = env.store.claim_job("dead-worker", lease_s=-1)  # a worker claimed it, then died
    assert job["status"] == "running"
    assert env.sms.recover() == 1
    job = env.store.job(job["id"])
    assert job["status"] == "queued" and "Interrupted" in job["error"]
    env.store.update_job(job["id"], next_run_at=0)
    env.drain()
    assert env.store.job(job["id"])["status"] == "succeeded"


def test_lost_lease_does_not_double_finish(env, monkeypatch):
    env.text(OWNER, "Build me a habit tracker")
    original = env.orch.run_existing

    def hijacked(build_id, listener=None):
        result = original(build_id, listener)
        jid = env.jobs()[0]["id"]
        env.store.update_job(jid, lease_owner="other-worker")
        return result

    monkeypatch.setattr(env.orch, "run_existing", hijacked)
    env.drain()
    assert env.jobs()[0]["status"] == "running"  # left for the worker that owns it now
    assert not any("is ready" in b for b in env.provider.bodies())


def test_cancel_queued_build(env):
    env.text(OWNER, "Build me a habit tracker")
    env.text(OWNER, "ABORT")
    assert "cancelled" in env.last()
    assert env.jobs()[0]["status"] == "cancelled"
    env.drain()
    assert env.orch.store.list() == []  # never started
    env.text(OWNER, "ABORT")
    assert "No build is running" in env.last()


class BlockingBuilder(FakeBuilder):
    def __init__(self, settings):
        super().__init__(settings)
        self.started = threading.Event()

    def run_gradle(self, workspace, log_path, on_line=None, task="assembleDebug", cancel=None):
        from genesis.core.builder import GradleResult

        self.started.set()
        assert cancel is not None and cancel.wait(10)
        return GradleResult(None, "", False, 0.1, cancelled=True)


def test_cancel_running_build(settings, monkeypatch):
    monkeypatch.setattr(worker_mod, "HEARTBEAT_S", 0.05)
    builder = BlockingBuilder(settings)
    env = Env(settings, builder=builder)
    env.text(OWNER, "Build me a habit tracker")
    t = threading.Thread(target=env.sms.run_once)
    t.start()
    assert builder.started.wait(5)
    env.text(OWNER, "CANCEL BUILD")  # bare CANCEL is a Twilio opt-out keyword
    assert "Stopping build" in env.last()
    t.join(10)
    job = env.jobs()[0]
    assert job["status"] == "cancelled"
    build = env.orch.store.get(job["build_id"])
    assert build["status"] == "failed" and build["cancelled"] is True
    assert "cancelled" in env.last()


# ---------- outbound delivery ----------
def test_transient_send_failure_is_retried(env):
    env.provider.failures = [SendError("Twilio 503", permanent=False)]
    env.text(OWNER, "HELP")
    env.sms.send_outbox()
    msg = env.store.one("SELECT * FROM messages WHERE direction = 'out'")
    assert msg["status"] == "pending" and msg["attempts"] == 1 and msg["next_attempt_at"] > time.time()
    env.store._conn().execute("UPDATE messages SET next_attempt_at = 0 WHERE id = ?", (msg["id"],))
    env.sms.send_outbox()
    assert env.store.one("SELECT status FROM messages WHERE id = ?", (msg["id"],))["status"] == "queued"


def test_permanent_send_failure_is_not_retried(env):
    env.provider.failures = [SendError("Twilio 400 error 21608", permanent=True, code=21608)]
    env.text(OWNER, "HELP")
    env.sms.send_outbox()
    msg = env.store.one("SELECT * FROM messages WHERE direction = 'out'")
    assert msg["status"] == "failed" and "21608" in msg["error"]


def test_twilio_provider_classifies_errors(settings, monkeypatch):
    from twilio.base.exceptions import TwilioRestException

    from genesis.sms.provider import TwilioProvider

    p = TwilioProvider(settings)

    class Messages:
        def __init__(self, exc):
            self.exc = exc

        def create(self, **kw):
            assert "from_" in kw and kw["status_callback"] == "https://cb"
            raise self.exc

    for exc, permanent in ((TwilioRestException(400, "u", msg="unverified", code=21608), True),
                           (TwilioRestException(503, "u", msg="down", code=None), False),
                           (TwilioRestException(429, "u", msg="slow", code=20429), False)):
        monkeypatch.setattr(p, "_get_client", lambda e=exc: type("C", (), {"messages": Messages(e)})())
        with pytest.raises(SendError) as info:
            p.send(OWNER, "hi", "https://cb")
        assert info.value.permanent is permanent
        assert settings.twilio_auth_token not in str(info.value)


def test_delivery_status_callback(env):
    env.text(OWNER, "HELP")
    env.sms.send_outbox()
    msg = env.store.one("SELECT * FROM messages WHERE direction = 'out'")
    path = "/api/webhooks/twilio/status"
    params = {"MessageSid": msg["provider_sid"], "MessageStatus": "undelivered", "ErrorCode": "30008",
              "AccountSid": env.settings.twilio_account_sid}
    r = env.client.post(path, content=urlencode(params), headers={
        "content-type": "application/x-www-form-urlencoded", "x-twilio-signature": env.sign(path, params)})
    assert r.status_code == 204
    msg = env.store.one("SELECT * FROM messages WHERE id = ?", (msg["id"],))
    assert msg["status"] == "undelivered" and msg["error"] == "30008"
    r = env.client.post(path, content=urlencode(params), headers={
        "content-type": "application/x-www-form-urlencoded", "x-twilio-signature": "bad"})
    assert r.status_code == 403


def test_outbound_messages_fit_three_segments(env):
    env.text(OWNER, "Build me a " + "very " * 300 + "long habit tracker")
    assert all(len(b) <= 450 and b.isascii() for b in env.provider.bodies())


# ---------- downloads ----------
def test_download_token_rules(env):
    env.text(OWNER, "Build me a habit tracker")
    env.drain()
    path = link_from(env.last()).removeprefix("https://genesis.example.test")
    assert env.client.get(path).status_code == 200
    assert env.client.get("/download/" + "A" * 32).status_code == 404
    assert env.client.get("/download/../../etc/passwd").status_code == 404
    env.store._conn().execute("UPDATE download_tokens SET expires_at = 0")
    r = env.client.get(path)
    assert r.status_code == 410 and "Text OPEN" in r.text
    env.text(OWNER, "OPEN")
    fresh = link_from(env.last()).removeprefix("https://genesis.example.test")
    assert fresh != path and env.client.get(fresh).status_code == 200


def test_download_limit_per_link(env, monkeypatch):
    from genesis.sms import routes

    env.text(OWNER, "Build me a habit tracker")
    env.drain()
    path = link_from(env.last()).removeprefix("https://genesis.example.test")
    for _ in range(routes.MAX_DOWNLOADS_PER_LINK):
        assert env.client.get(path).status_code == 200
    assert env.client.get(path).status_code == 410


def test_tokens_are_stored_hashed(env):
    env.text(OWNER, "Build me a habit tracker")
    env.drain()
    token = link_from(env.last()).rsplit("/", 1)[1]
    rows = env.store.all("SELECT token_hash FROM download_tokens")
    assert rows and all(token not in r["token_hash"] for r in rows)


def test_success_never_announced_without_artifact(env, monkeypatch):
    original = env.orch.run_existing

    def drop_apk(build_id, listener=None):
        result = original(build_id, listener)
        for f in (env.orch.store.path(build_id) / "artifact").iterdir():
            f.unlink()
        return result

    monkeypatch.setattr(env.orch, "run_existing", drop_apk)
    env.text(OWNER, "Build me a habit tracker")
    env.drain()
    assert env.jobs()[0]["status"] == "failed"
    assert not any("/download/" in b for b in env.provider.bodies())


def test_status_endpoint_is_local_and_secret_free(env):
    r = env.client.get("/api/sms/status")
    assert r.status_code == 200
    assert env.settings.twilio_auth_token not in r.text and OWNER not in r.text


# ---------- helpers ----------
@pytest.mark.parametrize("raw,expected", [
    ("+91 98765 43210", "+919876543210"),
    ("0091-98765-43210", "+919876543210"),
    ("098765 43210", "+919876543210"),
    ("(415) 555-0100", "+914155550100"),
    ("+1 415 555 0100", "+14155550100"),
    ("12", None), ("hello", None), ("", None), ("+0123456789", None),
])
def test_normalize_e164(raw, expected):
    assert normalize_e164(raw, "+91") == expected


def test_mask_hides_number():
    assert mask("+919876543210") == "+91******3210"


def test_compose_spec_keeps_newest_changes_within_limit():
    spec = compose_spec("A habit tracker", ["dark theme", "add reminders"])
    assert spec.index("dark theme") < spec.index("add reminders")
    long = compose_spec("A habit tracker", ["x" * 1500, "y" * 1500, "z" * 1500])
    assert "z" * 1500 in long and "x" * 1500 not in long and len(long) <= 4000


def test_whatsapp_replies_go_back_over_whatsapp(env):
    wa = "whatsapp:" + OWNER
    assert normalize_e164(wa) == OWNER
    env.text(wa, "Build me a habit tracker with streaks")
    assert env.user()["channel"] == "whatsapp"
    env.drain()
    bodies = env.provider.bodies(wa)
    assert "Build ID" in bodies[0] and "is ready" in bodies[-1] and env.provider.bodies(OWNER) == []
    env.text(wa, "CANCEL")  # not a WhatsApp opt-out word: just "no build is running"
    assert env.user()["status"] == "active" and "No build is running" in env.last(wa)
    env.text(OWNER, "STATUS")  # the same person over SMS: replies switch to SMS
    assert env.user()["channel"] == "sms" and "is ready" in env.last(OWNER)


def test_twilio_provider_picks_sender_by_address(settings, monkeypatch):
    from genesis.sms.provider import TwilioProvider

    settings.twilio_whatsapp_number = "+14155238886"
    sent = []

    class Messages:
        def create(self, **kw):
            sent.append(kw)
            return type("M", (), {"sid": "SM" + "e" * 32, "status": "queued"})()

    p = TwilioProvider(settings)
    monkeypatch.setattr(p, "_get_client", lambda: type("C", (), {"messages": Messages()})())
    p.send("whatsapp:" + OWNER, "hi", None)
    p.send(OWNER, "hi", None)
    assert sent[0]["to"] == "whatsapp:" + OWNER and sent[0]["from_"] == "whatsapp:+14155238886"
    assert sent[1]["to"] == OWNER and sent[1]["from_"] == settings.twilio_phone_number
    settings.twilio_phone_number = ""
    with pytest.raises(SendError) as info:
        p.send(OWNER, "hi", None)
    assert info.value.permanent and "TWILIO_PHONE_NUMBER" in str(info.value)
    settings.twilio_auth_token = ""
    with pytest.raises(SendError):
        TwilioProvider(settings).send("whatsapp:" + OWNER, "hi", None)


def test_replies_come_from_the_number_the_user_wrote_to(env):
    """WhatsApp only allows free-form replies from the number the user messaged (else error 21654)."""
    wa = "whatsapp:" + OWNER
    env.text(wa, "Build me a habit tracker", extra={"To": "whatsapp:+17372508034"})
    env.drain()
    assert env.user()["reply_from"] == "+17372508034"
    assert env.provider.senders and set(env.provider.senders) == {"+17372508034"}  # ack, progress, result
    code = env.store.create_link_code()
    env.text("whatsapp:" + STRANGER, f"LINK {code}", extra={"To": "whatsapp:+17372508034"})
    assert env.user(STRANGER)["reply_from"] == "+17372508034" and env.provider.senders[-1] == "+17372508034"
    env.text("whatsapp:+919876500077", "hello", extra={"To": "whatsapp:+17372508034"})  # unknown sender
    assert env.provider.senders[-1] == "+17372508034"


def test_twilio_provider_uses_explicit_sender(settings, monkeypatch):
    from genesis.sms.provider import TwilioProvider

    sent = []

    class Messages:
        def create(self, **kw):
            sent.append(kw)
            return type("M", (), {"sid": "SM" + "f" * 32, "status": "queued"})()

    p = TwilioProvider(settings)
    monkeypatch.setattr(p, "_get_client", lambda: type("C", (), {"messages": Messages()})())
    p.send("whatsapp:" + OWNER, "hi", None, sender="+17372508034")
    assert sent[0]["from_"] == "whatsapp:+17372508034"


def test_webhook_returns_text_xml(env):
    r = env.text(OWNER, "HELP")
    assert r.headers["content-type"].startswith("text/xml")


def test_whatsapp_replies_can_go_through_a_content_template(settings, monkeypatch):
    import json

    from genesis.sms.provider import TwilioProvider

    settings.twilio_whatsapp_content_sid = "HX" + "a" * 32
    sent = []

    class Messages:
        def create(self, **kw):
            sent.append(kw)
            return type("M", (), {"sid": "SM" + "f" * 32, "status": "queued"})()

    p = TwilioProvider(settings)
    monkeypatch.setattr(p, "_get_client", lambda: type("C", (), {"messages": Messages()})())
    p.send("whatsapp:" + OWNER, "Genesis: ready!\nhttps://x/download/abc", None, sender="+17372508034")
    p.send(OWNER, "plain sms", None)
    assert "body" not in sent[0] and sent[0]["content_sid"] == "HX" + "a" * 32
    assert json.loads(sent[0]["content_variables"]) == {"1": "Genesis: ready!\nhttps://x/download/abc"}
    assert sent[1]["body"] == "plain sms" and "content_sid" not in sent[1]


# ---------- referrals ----------
def test_invite_code_signs_up_friend_and_rewards_referrer(env):
    env.text(OWNER, "INVITE")
    code = env.store.referral_code(env.user(OWNER)["id"])
    assert f"JOIN {code}" in env.last(OWNER)
    env.text(STRANGER, f"join {code.lower()}")
    friend = env.user(STRANGER)
    assert friend["role"] == "guest" and friend["referred_by"] == env.user(OWNER)["id"]
    assert "Welcome to Weave" in env.last(STRANGER)
    assert "extra builds" in env.last(OWNER)
    assert env.user(OWNER)["bonus_builds"] == env.settings.sms_referral_bonus
    env.text(STRANGER, f"JOIN {code}")
    assert "already linked" in env.last(STRANGER)


def test_referral_code_counts_as_link_failure_when_wrong_and_respects_limits(env):
    env.text(OWNER, "INVITE")
    code = env.store.referral_code(env.user(OWNER)["id"])
    env.settings.sms_referral_max = 1
    env.text(STRANGER, f"JOIN {code}")
    other = "+919876500077"
    env.text(other, f"JOIN {code}")
    assert env.user(other) is None  # out of invites
    env.settings.sms_referrals = False
    env.settings.sms_referral_max = 20
    env.text("+919876500078", f"JOIN {code}")
    assert env.user("+919876500078") is None


def test_bonus_builds_raise_daily_limit(env):
    env.text(OWNER, "INVITE")
    code = env.store.referral_code(env.user(OWNER)["id"])
    env.text(STRANGER, f"JOIN {code}")
    friend = env.user(STRANGER)
    env.settings.sms_daily_builds_guest = 0
    env.store.update_user(friend["id"], bonus_builds=1)
    assert env.sms.service._gate(env.store.user_by_id(friend["id"])) is None
    env.store.update_user(friend["id"], bonus_builds=0)
    assert env.sms.service._gate(env.store.user_by_id(friend["id"])) == "quota"
