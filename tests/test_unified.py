"""One conversation, two builders: Android apps and websites through the same Twilio webhook, store and worker.
Gemini, Gradle, the weave bridge and Netlify are faked (see test_website.py for the real bridge contract)."""

import shutil
import time
import uuid
from pathlib import Path

import pytest
from conftest import FakeBuilder, FakeGemini, manifest
from fastapi.testclient import TestClient
from test_sms import OWNER, FakeProvider
from test_website import PAGE, FakeRunner, engine_dir, ok

from genesis.core.jobs import Orchestrator
from genesis.server import create_app
from genesis.sms.brain import ChatBrain, ChatDecision
from genesis.web.builder import WebsiteBuilder
from genesis.web.deploy import PublishError

MEMBER = "+919876500002"
STRANGER_SECRET = "nfp_secret_netlify_token"


class FakePublisher:
    def __init__(self, fail: str | None = None):
        self.calls = []
        self.fail = fail

    def __call__(self, token, site_dir, *, brand, site_id):
        self.calls.append({"token": token, "site_dir": Path(site_dir), "brand": brand, "site_id": site_id})
        if self.fail:
            raise PublishError(self.fail)
        return {"site_id": site_id or "site-1", "url": "https://genesis-brew.netlify.app", "deploy_id": "d1"}


class UEnv:
    def __init__(self, settings, tmp_path, results=None, websites=True, publisher=None):
        if websites:
            settings.website_engine_dir = str(engine_dir(tmp_path))
            settings.node_bin = shutil.which("node") or str(Path(shutil.which("python3") or "/bin/sh"))
        self.settings = settings
        self.gemini = FakeGemini(manifest())
        self.orch = Orchestrator(settings, self.gemini, builder=FakeBuilder(settings))
        self.runner = FakeRunner(results or [])
        self.web = WebsiteBuilder(settings, runner=self.runner)
        self.publisher = publisher or FakePublisher()
        self.provider = FakeProvider(settings)
        self.app = create_app(settings, orchestrator=self.orch, sms_provider=self.provider, start_sms_worker=False,
                              website_builder=self.web, publisher=self.publisher)
        self.client = TestClient(self.app)
        self.sms = self.app.state.sms
        self.store = self.sms.store

    def text(self, frm, body, sid=None):
        from test_sms import Env
        return Env.text(self, frm, body, sid=sid)

    def sign(self, path, params):
        from test_sms import Env
        return Env.sign(self, path, params)

    def drain(self):
        self.sms.drain()

    def last(self, to=OWNER):
        self.sms.send_outbox()
        bodies = self.provider.bodies(to)
        return bodies[-1] if bodies else None

    def all(self, to=OWNER):
        self.sms.send_outbox()
        return self.provider.bodies(to)

    def user(self, phone=OWNER):
        return self.store.get_user(phone)

    def jobs(self, phone=OWNER):
        u = self.user(phone)
        return self.store.all("SELECT * FROM jobs WHERE user_id = ? ORDER BY id", (u["id"],)) if u else []

    def projects(self, phone=OWNER):
        u = self.user(phone)
        return self.store.all("SELECT * FROM projects WHERE user_id = ? ORDER BY number", (u["id"],)) if u else []

    def link_member(self, phone=MEMBER):
        code = self.store.create_link_code("member")
        self.text(phone, f"LINK {code}")


@pytest.fixture
def u(settings, tmp_path):
    return UEnv(settings, tmp_path, results=[ok(), ok(html=PAGE.replace("Brew Haven", "Brew Haven v2")), ok()])


def preview_link(text):
    return next(w for w in text.split() if "/preview/" in w)


def path_of(url):
    return url.split("genesis.example.test", 1)[1]


# ---------- routing ----------
def test_greeting_and_status_question_never_build(u):
    u.text(OWNER, "hi")
    assert "Android app or a website" in u.last()
    u.text(OWNER, "What are you building?")
    assert "No builds yet" in u.last()
    assert u.jobs() == []


def test_android_request_goes_to_android_builder(u):
    u.text(OWNER, "Build an Android inventory app with stock counts")
    assert u.projects()[0]["kind"] == "android"
    u.drain()
    assert "/download/" in u.last() and "/preview/" not in u.last()
    assert u.runner.requests == []


def test_website_request_goes_to_website_builder_and_delivers_private_preview(u):
    u.text(OWNER, "Create a premium website for my coffee brand")
    assert u.projects()[0]["kind"] == "website"
    assert "designing your website" in u.last()
    u.drain()
    done = u.last()
    assert "Your website Brew Haven is ready!" in done
    assert "not published" in done and "netlify" not in done.lower()
    assert u.gemini.prompts == []  # the Android pipeline was not used
    r = u.client.get(path_of(preview_link(done)))
    assert r.status_code == 200 and "Brew Haven" in r.text
    csp = r.headers["content-security-policy"]
    assert csp.startswith("sandbox allow-scripts") and "allow-same-origin" not in csp
    assert "connect-src 'none'" in csp and "form-action 'none'" in csp
    assert r.headers["cache-control"] == "private, no-store" and "noindex" in r.headers["x-robots-tag"]


def test_progress_messages_reflect_real_website_stages(u):
    u.text(OWNER, "Create a premium website for my coffee brand")
    u.drain()
    texts = "\n".join(u.all())
    assert "Researching a design direction" in texts
    assert "%" not in texts


def test_unclear_target_asks_android_or_website(u):
    u.text(OWNER, "Build something for my bakery with opening hours")
    assert "phone app" in u.last() and "website" in u.last()
    assert u.jobs() == []
    u.text(OWNER, "website")
    assert u.projects()[0]["kind"] == "website" and len(u.jobs()) == 1
    assert u.projects()[0]["original_prompt"] == "Build something for my bakery with opening hours"


def test_web_app_is_ambiguous_and_can_become_an_android_app(u):
    u.text(OWNER, "I want a web app for tracking invoices")
    assert "Reply \"app\" or \"website\"" in u.last()
    u.text(OWNER, "an app please")
    assert u.projects()[0]["kind"] == "android"


def test_status_question_reports_job_without_new_job(u):
    u.text(OWNER, "Create a premium website for my coffee brand")
    u.text(OWNER, "What are you building?")
    assert "waiting for a build slot" in u.last()
    u.text(OWNER, "how's it going?")
    assert len(u.jobs()) == 1


def test_modification_applies_to_the_active_website(u):
    u.text(OWNER, "Create a premium website for my coffee brand")
    u.drain()
    v1 = u.projects()[0]["last_success_build_id"]
    u.text(OWNER, "Make the buttons bigger")
    job = u.jobs()[-1]
    assert job["kind"] == "change" and job["base_build_id"] == v1 and job["project_id"] == u.projects()[0]["id"]
    u.drain()
    assert "Applying your change to the website." in "\n".join(u.all())
    assert u.runner.requests[-1]["op"] == "modify" and u.runner.requests[-1]["feedback"] == "Make the buttons bigger"
    assert "is updated!" in u.last()
    v2 = u.projects()[0]["last_success_build_id"]
    assert v2 != v1 and "v2" in (u.web.site_dir(v2) / "index.html").read_text()
    assert len(u.projects()) == 1


def test_project_selection_routes_changes_to_the_right_builder(u):
    u.text(OWNER, "Build an Android inventory app with stock counts")
    u.drain()
    u.text(OWNER, "new app: Create a premium website for my coffee brand")
    u.drain()
    assert [p["kind"] for p in u.projects()] == ["android", "website"]
    u.text(OWNER, "show my projects")
    listing = u.last()
    assert "2 Brew Haven (website) - ready *" in listing and "1 Lift Log - ready" in listing
    u.text(OWNER, "switch to 1")
    u.text(OWNER, "make the theme dark")
    u.drain()
    assert len(u.gemini.modify_calls) == 1  # Android change through the Android pipeline
    assert [r["op"] for r in u.runner.requests] == ["generate"]
    u.text(OWNER, "USE 2")
    u.text(OWNER, "make the hero darker")
    u.drain()
    assert [r["op"] for r in u.runner.requests] == ["generate", "modify"]


def test_send_the_link_for_a_website_gives_preview_not_apk(u):
    u.text(OWNER, "Create a premium website for my coffee brand")
    u.drain()
    u.text(OWNER, "send the link")
    text = u.last()
    assert "/preview/" in text and "/download/" not in text and "Not published." in text


# ---------- ownership and tokens ----------
def test_other_users_cannot_see_or_use_projects(u):
    u.text(OWNER, "Create a premium website for my coffee brand")
    u.drain()
    u.link_member()
    u.text(MEMBER, "USE 1")
    assert "couldn't find" in u.last(MEMBER)
    u.text(MEMBER, "make the buttons bigger")
    assert u.jobs(MEMBER) == []


def test_preview_and_download_tokens_do_not_cross(u):
    u.text(OWNER, "Build an Android inventory app with stock counts")
    u.drain()
    apk_token = next(w for w in u.last().split() if "/download/" in w).rsplit("/", 1)[1]
    u.text(OWNER, "new app: Create a premium website for my coffee brand")
    u.drain()
    site_token = preview_link(u.last()).rstrip("/").rsplit("/", 1)[1]
    assert u.client.get(f"/preview/{apk_token}/").status_code == 404
    assert u.client.get(f"/download/{site_token}").status_code == 404
    assert u.client.get(f"/preview/{site_token}/../job.json").status_code == 404
    assert u.client.get(f"/preview/{site_token}/build.log").status_code == 404
    assert u.client.get(f"/preview/{'x' * 32}/").status_code == 404


def test_duplicate_webhook_delivery_creates_one_website_job(u):
    sid = "SM" + uuid.uuid4().hex
    u.text(OWNER, "Create a premium website for my coffee brand", sid=sid)
    u.text(OWNER, "Create a premium website for my coffee brand", sid=sid)
    assert len(u.jobs()) == 1 and len(u.projects()) == 1


def test_second_request_while_building_is_refused(u):
    u.text(OWNER, "Create a premium website for my coffee brand")
    u.text(OWNER, "new app: Build an Android inventory app with stock counts")
    assert "still in progress" in u.last()
    assert len(u.jobs()) == 1


# ---------- failures and rollback ----------
def test_failed_website_change_keeps_last_good_version(settings, tmp_path):
    u = UEnv(settings, tmp_path, results=[ok(), {"ok": False, "error": "Gemini API error (429): quota exceeded"}, ok()])
    u.text(OWNER, "Create a premium website for my coffee brand")
    u.drain()
    good = u.projects()[0]["last_success_build_id"]
    u.text(OWNER, "make the hero darker")
    u.drain()
    assert "failed" in u.last() and "out of quota" in u.last()
    assert u.projects()[0]["last_success_build_id"] == good
    u.text(OWNER, "send the link")
    assert "/preview/" in u.last()
    u.text(OWNER, "try again")
    u.drain()
    assert u.runner.requests[-1]["op"] == "modify" and u.runner.requests[-1]["base_dir"].endswith(f"{good}/site")
    assert u.projects()[0]["last_success_build_id"] != good


def test_website_requests_are_refused_honestly_when_unavailable(settings, tmp_path):
    u = UEnv(settings, tmp_path, websites=False)
    u.text(OWNER, "Create a premium website for my coffee brand")
    assert "can't make websites" in u.last()
    assert u.jobs() == [] and u.projects() == []
    u.text(OWNER, "hi")
    assert "website" not in u.last().lower()
    u.text(OWNER, "Build something for my bakery with opening hours")
    assert u.projects()[0]["kind"] == "android"  # only one builder: no question to ask


# ---------- publishing ----------
@pytest.fixture
def pub(settings, tmp_path):
    settings.website_deploy = "netlify"
    settings.netlify_auth_token = STRANGER_SECRET
    e = UEnv(settings, tmp_path, results=[ok(), ok()])
    e.text(OWNER, "Create a premium website for my coffee brand")
    e.drain()
    return e


def test_publish_needs_explicit_confirmation(pub):
    assert "publish it" in pub.last()
    pub.text(OWNER, "publish it")
    assert "Reply PUBLISH to confirm" in pub.last() and "public internet" in pub.last()
    assert pub.publisher.calls == [] and pub.jobs()[-1]["kind"] != "publish"
    pub.text(OWNER, "PUBLISH")
    assert pub.jobs()[-1]["kind"] == "publish"
    pub.drain()
    call = pub.publisher.calls[0]
    assert call["token"] == STRANGER_SECRET and call["site_dir"] == pub.web.site_dir(pub.projects()[0]["last_success_build_id"])
    assert "Published! Brew Haven is live at https://genesis-brew.netlify.app" in pub.last()
    assert pub.projects()[0]["deploy_url"] == "https://genesis-brew.netlify.app"
    assert all(STRANGER_SECRET not in b for b in pub.all())


def test_declining_publish_does_nothing(pub):
    pub.text(OWNER, "publish it")
    pub.text(OWNER, "no")
    assert "not publishing" in pub.last()
    pub.drain()
    assert pub.publisher.calls == []


def test_yes_is_not_a_publish_approval(pub):
    pub.text(OWNER, "deploy my website")
    pub.text(OWNER, "yes")
    pub.drain()
    assert pub.publisher.calls == []


def test_members_cannot_publish(pub):
    pub.link_member()
    pub.text(MEMBER, "Create a premium website for my coffee brand")
    pub.drain()
    pub.text(MEMBER, "publish it")
    assert ("Only the Weave owner" in pub.last(MEMBER) or "Only the Genesis owner" in pub.last(MEMBER))
    pub.text(MEMBER, "PUBLISH")
    pub.drain()
    assert pub.publisher.calls == []


def test_publishing_off_by_default_and_never_anonymous(u):
    u.text(OWNER, "Create a premium website for my coffee brand")
    u.drain()
    assert "publish it" not in u.last()
    u.text(OWNER, "publish it")
    assert "isn't turned on" in u.last()
    u.text(OWNER, "PUBLISH")
    u.drain()
    assert u.publisher.calls == []


def test_android_apps_cannot_be_published(pub):
    pub.text(OWNER, "new app: Build an Android inventory app with stock counts")
    pub.drain()
    pub.text(OWNER, "USE 2")
    pub.text(OWNER, "publish it")
    assert "Only websites can be published" in pub.last()


def test_publish_failure_is_reported_and_preview_kept(settings, tmp_path):
    settings.website_deploy = "netlify"
    settings.netlify_auth_token = STRANGER_SECRET
    e = UEnv(settings, tmp_path, results=[ok()], publisher=FakePublisher(fail="Netlify rejected the token"))
    e.text(OWNER, "Create a premium website for my coffee brand")
    e.drain()
    e.text(OWNER, "publish it")
    e.text(OWNER, "PUBLISH")
    e.drain()
    assert "publishing failed (Netlify rejected the token)" in e.last()
    assert e.projects()[0]["deploy_url"] is None
    e.text(OWNER, "try again")
    assert "publish it" in e.last() and len(e.publisher.calls) == 1


# ---------- AI chat brain over both builders ----------
class FakeAI:
    def __init__(self, *decisions):
        self.decisions = list(decisions)
        self.systems = []
        self.contexts = []

    def ask(self, system, user, schema, title="", timeout=None):
        self.systems.append(system)
        self.contexts.append(user)
        return self.decisions.pop(0), {"seconds": 0.1}


def with_brain(env, *decisions):
    env.ai = FakeAI(*decisions)
    env.sms.service.brain = ChatBrain(env.settings, env.ai, synchronous=True,
                                      website_problem=env.sms.service.website_problem)
    return env


def test_brain_cannot_start_a_build_from_a_greeting(u):
    with_brain(u, ChatDecision(reply="Building!", action="new_app", spec="a habit tracker app"))
    u.text(OWNER, "hey")
    assert u.jobs() == []


def test_brain_new_website_and_context_lists_kinds(u):
    with_brain(u, ChatDecision(reply="On it!", action="new_website", spec="A premium website for Brew Haven coffee"))
    u.text(OWNER, "so I run a coffee brand and honestly a premium website would help a lot")
    assert u.projects()[0]["kind"] == "website"
    assert "new_website" in u.ai.systems[0] and "Website builder: available" in u.ai.contexts[0]


def test_brain_disagreeing_with_user_words_asks(u):
    with_brain(u, ChatDecision(reply="Building your app", action="new_app", spec="Coffee brand landing"))
    u.text(OWNER, "so I run a coffee brand and honestly a premium website would help a lot")
    assert u.jobs() == [] and "Reply \"app\" or \"website\"" in u.last()


def test_brain_publish_action_only_asks(pub):
    with_brain(pub, ChatDecision(reply="Published!", action="publish"))
    pub.text(OWNER, "put my site online")
    assert "Reply PUBLISH to confirm" in pub.last()
    pub.drain()
    assert pub.publisher.calls == []


def test_brain_told_websites_unavailable(settings, tmp_path):
    e = with_brain(UEnv(settings, tmp_path, websites=False),
                   ChatDecision(reply="Sure!", action="new_website", spec="A website for my coffee brand"))
    e.text(OWNER, "so I run a coffee brand and honestly a premium website would help a lot")
    assert "Never use new_website" in e.ai.systems[0]
    assert e.jobs() == [] and "can't make websites" in e.last()


# ---------- web chat shares the same projects ----------
def test_web_chat_state_shows_project_kind(u):
    u.text(OWNER, "Create a premium website for my coffee brand")
    u.drain()
    owner = u.user()
    token = u.store.create_web_session(owner["id"], 3600)
    from genesis.sms.chat import COOKIE
    u.client.cookies.set(COOKIE, token)
    state = u.client.get("/api/chat/state").json()
    assert state["projects"][0]["kind"] == "website" and state["build"]["status"] == "succeeded"


# ---------- schema migration ----------
def test_old_database_migrates_additively(tmp_path):
    import sqlite3

    from genesis.sms.store import SmsStore

    path = tmp_path / "old.sqlite3"
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, phone TEXT NOT NULL UNIQUE, role TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active', active_project_id INTEGER, awaiting_new INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, linked_at REAL);
        CREATE TABLE projects (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, number INTEGER NOT NULL, name TEXT,
            original_prompt TEXT NOT NULL, changes TEXT NOT NULL DEFAULT '[]', last_success_build_id TEXT,
            created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE (user_id, number));
        CREATE TABLE messages (id INTEGER PRIMARY KEY, user_id INTEGER, phone TEXT NOT NULL, direction TEXT NOT NULL,
            body TEXT NOT NULL, provider_sid TEXT, status TEXT NOT NULL, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0, job_id INTEGER,
            created_at REAL NOT NULL, updated_at REAL NOT NULL);
        INSERT INTO users (phone, role, created_at) VALUES ('+919876500001', 'owner', 1);
        INSERT INTO projects (user_id, number, name, original_prompt, last_success_build_id, created_at, updated_at)
            VALUES (1, 1, 'Lift Log', 'A workout log app', 'abc', 1, 1);
    """)
    c.commit()
    c.close()
    store = SmsStore(path)
    p = store.project_by_number(1, 1)
    assert p["kind"] == "android" and p["name"] == "Lift Log" and p["last_success_build_id"] == "abc"
    assert p["deploy_url"] is None
    assert store.get_user("+919876500001")["pending_question"] is None
    SmsStore(path)  # idempotent
    assert store.create_project(1, "A site for my bakery", "website")["number"] == 2
    with pytest.raises(ValueError):
        store.create_project(1, "x" * 20, "ios")


# ---------- responsiveness when the AI is slow ----------
class SlowAI:
    def __init__(self, delay):
        self.delay = delay
        self.calls = 0

    def ask(self, system, user, schema, title="", timeout=None):
        import time
        self.calls += 1
        time.sleep(self.delay)
        return ChatDecision(reply="late"), {}


def test_simple_messages_never_wait_for_the_ai(u):
    u.ai = SlowAI(30)
    u.sms.service.brain = ChatBrain(u.settings, u.ai, synchronous=True, website_problem=u.sms.service.website_problem)
    for text in ("What are you building?", "show my projects", "send the link", "cancel it"):
        u.text(OWNER, text)
    assert u.ai.calls == 0 and len(u.all()) == 4


def test_slow_ai_falls_back_to_rules(u):
    u.settings.chat_timeout = 1
    u.ai = SlowAI(5)
    u.sms.service.brain = ChatBrain(u.settings, u.ai, synchronous=True, website_problem=u.sms.service.website_problem)
    u.text(OWNER, "so I run a coffee brand and honestly a premium website would help a lot")
    assert u.ai.calls == 1
    assert u.projects()[0]["kind"] == "website" and "designing your website" in u.last()


@pytest.mark.parametrize("text", ["hows it going", "how's it going?", "how is my website going", "whats happening"])
def test_status_spellings_are_instant(u, text):
    u.ai = SlowAI(30)
    u.sms.service.brain = ChatBrain(u.settings, u.ai, synchronous=True, website_problem=u.sms.service.website_problem)
    u.text(OWNER, text)
    assert u.ai.calls == 0 and "No builds yet" in u.last()


# ---------- asking for the other kind while a project is active ----------
APK_ASK = "can u also make me a phone apk version of ts so i can order coffee from this"


@pytest.mark.parametrize("brain", [False, True])
def test_apk_request_on_a_website_builds_a_new_android_app(u, brain):
    u.text(OWNER, "Create a premium website for my coffee brand")
    u.drain()
    site = u.projects()[0]
    if brain:  # the model wrongly calls it a change; the server still makes an app
        with_brain(u, ChatDecision(reply="Sure!", action="change_app", spec=APK_ASK))
    u.text(OWNER, APK_ASK)
    projects = u.projects()
    assert [p["kind"] for p in projects] == ["website", "android"]
    assert "Brew Haven" in projects[1]["original_prompt"] and APK_ASK in projects[1]["original_prompt"]
    u.drain()
    assert "/download/" in u.last() and "/preview/" not in u.last()
    assert [r["op"] for r in u.runner.requests] == ["generate"]  # the website was not touched
    assert u.projects()[0]["last_success_build_id"] == site["last_success_build_id"]


def test_real_website_edits_mentioning_apps_stay_website_changes(u):
    u.text(OWNER, "Create a premium website for my coffee brand")
    u.drain()
    u.text(OWNER, "add a button to download our phone app")
    assert u.jobs()[-1]["kind"] == "change" and len(u.projects()) == 1


def test_website_request_on_an_android_app_builds_a_new_website(u):
    u.text(OWNER, "Build an Android inventory app with stock counts")
    u.drain()
    u.text(OWNER, "now make a website for this app")
    assert [p["kind"] for p in u.projects()] == ["android", "website"]


@pytest.mark.parametrize("text", [APK_ASK, "can u make me a phone apk version of this so i can order coffee from this",
                                  "could you build an android app of this?"])
def test_apk_request_phrased_as_question_still_builds_the_app(u, text):
    u.settings.chat_timeout = 1
    u.text(OWNER, "Create a premium website for my coffee brand")
    u.drain()
    with_brain(u)
    u.ai.ask = SlowAI(5).ask  # the AI times out, so the built-in rules decide
    u.text(OWNER, text)
    assert [p["kind"] for p in u.projects()] == ["website", "android"]
    u.drain()
    assert "/download/" in u.last()


@pytest.mark.parametrize("text", ["yo whatsup", "hey bro whats up", "how are you", "hello there genesis!"])
def test_casual_greetings_are_greetings(u, text):
    u.text(OWNER, text)
    assert (u.last().startswith("Hey!") or u.last().startswith("Genesis: Hey!")) and u.jobs() == []


def test_greetings_are_answered_instantly_without_the_ai(u):
    with_brain(u, ChatDecision(reply="Yo! Not much, just here to build stuff. Got an idea?"))
    u.text(OWNER, "yo hi")
    assert "I'm Weave" in u.last() and u.jobs() == []  # fixed reply: never waits on an overloaded AI


# ---------- Weave hosting (<name>.<sites_domain>) ----------
@pytest.fixture
def hosted(settings, tmp_path):
    settings.website_deploy = "weave"
    settings.sites_domain = "weave.xyz"
    return UEnv(settings, tmp_path, results=[ok(), ok(html=PAGE.replace("Brew Haven", "Brew Haven v2")), ok()])


def test_every_website_goes_live_on_its_own_subdomain(hosted):
    hosted.text(OWNER, "Create a premium website for my coffee brand")
    hosted.drain()
    done = hosted.last()
    assert "https://brew-haven.weave.xyz/" in done and "live" in done
    assert hosted.projects()[0]["deploy_url"] == "https://brew-haven.weave.xyz/"
    assert hosted.publisher.calls == []  # never Netlify
    r = hosted.client.get("/", headers={"host": "brew-haven.weave.xyz"})
    assert r.status_code == 200 and "Brew Haven" in r.text
    assert r.headers["content-security-policy"].startswith("sandbox allow-scripts")
    assert hosted.client.get("/index.html", headers={"host": "brew-haven.weave.xyz"}).status_code == 200


def test_a_change_updates_the_same_subdomain(hosted):
    hosted.text(OWNER, "Create a premium website for my coffee brand")
    hosted.drain()
    hosted.text(OWNER, "make the header darker")
    hosted.drain()
    assert hosted.projects()[0]["deploy_url"] == "https://brew-haven.weave.xyz/"
    r = hosted.client.get("/", headers={"host": "brew-haven.weave.xyz"})
    assert "Brew Haven v2" in r.text


def test_hosted_sites_never_reach_the_api_or_other_files(hosted):
    hosted.text(OWNER, "Create a premium website for my coffee brand")
    hosted.drain()
    h = {"host": "brew-haven.weave.xyz"}
    for path in ("/api/builds", "/api/health", "/../sms.db", "/chat", "/job.json"):
        assert hosted.client.get(path, headers=h).status_code == 404, path
    assert hosted.client.post("/api/builds", headers=h, json={}).status_code == 405
    assert hosted.client.get("/", headers={"host": "nope.weave.xyz"}).status_code == 404
    assert hosted.client.get("/", headers={"host": "www.weave.xyz"}, follow_redirects=False).status_code != 200
    assert hosted.client.get("/", headers={"host": "a.b.weave.xyz"}, follow_redirects=False).status_code != 200


def test_same_brand_names_get_different_subdomains(tmp_path):
    from genesis.web.hosting import publish_hosted, site_label

    site = tmp_path / "s"
    site.mkdir()
    (site / "index.html").write_text("<html><body>x</body></html>")
    root = tmp_path / "hosted"
    first = site_label(root, "Brew Haven")
    publish_hosted(root, site, first)
    second = site_label(root, "Brew Haven")
    assert first == "brew-haven" and second.startswith("brew-haven-") and second != first
    assert site_label(root, "WWW") != "www" and site_label(root, "!!") .startswith("site")


# ---------- style question before a new website ----------
def test_website_asks_style_then_builds_with_the_answer(u):
    u.settings.website_ask_style = True
    u.text(OWNER, "Create a premium website for my coffee brand")
    assert "Which style do you like?" in u.last() and u.jobs() == []
    u.text(OWNER, "3, gold accents and a serif font")
    jobs = u.jobs()
    assert len(jobs) == 1 and "dark and premium, gold accents and a serif font" in jobs[0]["prompt"]
    assert jobs[0]["prompt"].startswith("Create a premium website for my coffee brand")


def test_style_question_can_be_skipped_or_cancelled(u):
    u.settings.website_ask_style = True
    u.text(OWNER, "Create a premium website for my coffee brand")
    u.text(OWNER, "skip")
    assert u.jobs()[0]["prompt"] == "Create a premium website for my coffee brand"
    u.drain()
    u.text(OWNER, "new website: a site for my yoga studio in Pune")
    assert "Which style" in u.last()
    u.text(OWNER, "never mind")  # bare CANCEL is an SMS opt-out keyword
    assert len(u.jobs()) == 1 and "won't build" in u.last()


def test_request_that_already_describes_the_look_is_not_asked(u):
    u.settings.website_ask_style = True
    u.text(OWNER, "Create a website for my coffee brand with a dark theme and gold colours")
    assert len(u.jobs()) == 1 and "Which style" not in u.last()


# ---------- build lanes ----------
def test_build_lanes_keep_websites_and_android_apart(u):
    store = u.store
    a = store.create_user("+15550001111", "member", "sms")
    b = store.create_user("+15550002222", "member", "sms")
    web = store.create_project(a["id"], "a website for my bakery", "website")
    app = store.create_project(b["id"], "a habit tracker app", "android")
    for user, project in ((a, web), (b, app)):
        store.enqueue_job(user_id=user["id"], project_id=project["id"], kind="new", prompt=project["original_prompt"],
                          change_request=None, base_build_id=None, max_attempts=1, inbound_sid=None)
    assert store.claim_job("w", 60, "android")["project_id"] == app["id"]
    assert store.claim_job("w", 60, "android") is None
    assert store.claim_job("w", 60, "website")["project_id"] == web["id"]


def test_home_page_goes_live_before_the_extra_pages(settings, tmp_path):
    settings.website_deploy = "weave"
    settings.sites_domain = "weave.xyz"
    holder = {}

    def slow_engine(request):
        out = Path(request["out_dir"])
        (out / "index.html").write_text(PAGE.replace("<head>", "<head><title>Brew Haven</title>"), encoding="utf-8")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (settings.hosted_dir / "brew-haven" / "index.html").exists():
            time.sleep(0.1)
        holder["live_early"] = (settings.hosted_dir / "brew-haven" / "index.html").exists()
        holder["sent_early"] = list(env.all())
        (out / "about.html").write_text(PAGE.replace("Brew Haven", "About us"), encoding="utf-8")
        (out / "menu.html").write_text("<html><body><iframe src=x></iframe></body></html>", encoding="utf-8")
        return ok(pages=[{"file": "index.html"}, {"file": "about.html"}, {"file": "menu.html"}])

    env = UEnv(settings, tmp_path, results=[slow_engine])
    env.text(OWNER, "Create a premium website for my coffee brand")
    env.drain()
    assert holder["live_early"] and any("home page is live: https://brew-haven.weave.xyz/" in m for m in holder["sent_early"])
    assert "All pages of Brew Haven are up" in env.last()
    assert not any("Researching" in m for m in env.all()[-2:])
    files = sorted(p.name for p in (settings.hosted_dir / "brew-haven").iterdir())
    assert files == ["about.html", "index.html"]  # the page that failed the checks was dropped, not the site


def test_clear_website_requests_skip_the_ai(u):
    with_brain(u, ChatDecision(reply="On it!", action="new_website", spec="unused"))
    u.text(OWNER, "Fitness website")
    assert len(u.ai.decisions) == 1 and u.projects()[0]["kind"] == "website"  # the AI was never asked


def test_bare_website_asks_what_for_then_builds(u):
    u.text(OWNER, "Website")
    assert "What's the website for?" in u.last() and u.jobs() == []
    u.text(OWNER, "my fitness coaching")
    assert u.projects()[0]["kind"] == "website" and "fitness coaching website" in u.jobs()[0]["prompt"]


def test_change_during_a_build_is_queued_and_applied_after(u):
    u.text(OWNER, "Create a premium website for my coffee brand")
    first = u.jobs()[0]
    u.store.update_job(first["id"], status="running")
    u.text(OWNER, "make the header darker")
    assert "right after it finishes" in u.last()
    u.text(OWNER, "and add a menu section")
    assert "apply all your changes together" in u.last()
    jobs = u.jobs()
    assert len(jobs) == 2 and jobs[1]["base_build_id"] is None and "add a menu" in jobs[1]["change_request"]
    u.store.update_job(first["id"], status="queued")
    u.drain()
    jobs = u.jobs()
    assert [j["status"] for j in jobs] == ["succeeded", "succeeded"]
    built_on = u.web.store.get(jobs[1]["build_id"]).get("base_job_id")
    assert built_on == jobs[0]["build_id"]  # applied on top of the first build, not from scratch
