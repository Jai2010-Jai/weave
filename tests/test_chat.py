"""Genesis Chat (mobile web): auth, CSRF, isolation, and the full build/change/download flow on the shared engine."""

import uuid

import pytest
from fastapi.testclient import TestClient
from test_sms import Env

H = {"X-Genesis-Chat": "1"}
TUNNEL = {"host": "genesis.example.test", "x-forwarded-for": "198.51.100.7", "x-forwarded-proto": "https"}


@pytest.fixture
def env(settings):
    return Env(settings)


def phone_client(env):
    """A browser on a phone, reaching Genesis through the HTTPS tunnel."""
    return TestClient(env.app, base_url="https://genesis.example.test", headers=TUNNEL)


def sign_in(env, client=None):
    client = client or phone_client(env)
    code = env.store.create_link_code()
    r = client.post("/api/chat/session", json={"code": code}, headers=H)
    assert r.status_code == 200, r.text
    return client


def say(client, text, client_id=None):
    return client.post("/api/chat/messages", json={"text": text, "client_id": client_id or uuid.uuid4().hex}, headers=H)


def genesis_texts(client):
    return [m["text"] for m in client.get("/api/chat/state").json()["messages"] if m["from"] == "genesis"]


def test_page_and_assets_are_public_with_strict_csp(env):
    c = phone_client(env)
    r = c.get("/chat")
    assert r.status_code == 200 and ("Weave Chat" in r.text or "Genesis Chat" in r.text)
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    for name in ("app.js", "app.css", "manifest.webmanifest", "sw.js", "icon.svg"):
        assert c.get(f"/chat/{name}").status_code == 200
    assert c.get("/chat/../server.py").status_code in (401, 404)
    assert c.get("/chat/secret.txt").status_code == 404
    assert c.get("/api/chat/state").status_code == 401
    assert c.get("/", headers=TUNNEL, follow_redirects=False).headers.get("location") == "/start"  # dashboard stays private


def test_sign_in_rules(env):
    c = phone_client(env)
    code = env.store.create_link_code()
    assert c.post("/api/chat/session", json={"code": code}).status_code == 403  # no CSRF header
    assert c.post("/api/chat/session", json={"code": "WRONGCODE12"}, headers=H).status_code == 401
    assert c.post("/api/chat/session", json={"local": True}, headers=H).status_code == 403  # proxied => not local
    r = c.post("/api/chat/session", json={"code": code}, headers=H)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "secure" in cookie
    assert c.get("/api/chat/state").status_code == 200
    other = phone_client(env)
    assert other.post("/api/chat/session", json={"code": code}, headers=H).status_code == 401  # single use
    c.post("/api/chat/logout", headers=H)
    assert c.get("/api/chat/state").status_code == 401


def test_local_browser_can_sign_in_without_code(env):
    local = TestClient(env.app)
    assert local.post("/api/chat/session", json={"local": True}, headers=H).status_code == 200
    assert local.get("/api/chat/state").status_code == 200
    assert env.store.get_user("web:" + "0" * 32)["role"] == "owner"


def test_wrong_codes_are_rate_limited(env):
    c = phone_client(env)
    codes = [c.post("/api/chat/session", json={"code": f"BAD{i:07d}"}, headers=H).status_code for i in range(21)]
    assert codes[:20] == [401] * 20 and codes[-1] == 429


def test_chat_build_change_and_download(env):
    c = sign_in(env)
    r = say(c, "Build me a habit tracker with streaks, reminders, and a dark theme")
    assert r.status_code == 200 and r.json()["outcome"] == "queued_new"
    state = c.get("/api/chat/state").json()
    assert state["build"]["status"] == "queued" and state["messages"][0]["from"] == "me"
    assert "Build ID" in state["messages"][1]["text"] and not state["messages"][1]["text"].startswith("Genesis:")

    env.drain()
    texts = genesis_texts(c)
    assert any("writing the app's code" in t for t in texts) and "is ready" in texts[-1]
    link = next(w for w in texts[-1].split() if "/download/" in w)
    assert link.startswith("https://genesis.example.test/download/")
    apk = c.get(link.removeprefix("https://genesis.example.test"))
    assert apk.status_code == 200 and apk.headers["content-type"] == "application/vnd.android.package-archive"
    assert env.provider.sent == []  # web replies never go to Twilio

    assert say(c, "Add a settings screen").json()["outcome"] == "queued_change"
    env.drain()
    projects = c.get("/api/chat/state").json()["projects"]
    assert len(projects) == 1 and projects[0]["ready"] and projects[0]["active"]
    assert env.gemini.modify_calls[-1][0] == "Add a settings screen"

    assert say(c, "Build another app: a tip calculator that splits the bill").json()["outcome"] == "queued_new"
    env.drain()
    assert len(c.get("/api/chat/state").json()["projects"]) == 2


def test_relative_links_without_public_url(env):
    env.settings.public_base_url = ""
    local = TestClient(env.app)
    local.post("/api/chat/session", json={"local": True}, headers=H)
    say(local, "Build me a habit tracker")
    env.drain()
    last = genesis_texts(local)[-1]
    link = next(w for w in last.split() if w.startswith("/download/"))
    assert local.get(link).status_code == 200


def test_cancel_is_a_command_in_chat(env):
    c = sign_in(env)
    say(c, "Build me a habit tracker")
    assert say(c, "CANCEL").json()["outcome"] == "cancel_cancelled"
    user = env.store.all("SELECT * FROM users WHERE channel = 'web'")[0]
    assert user["status"] == "active"


def test_chat_users_are_isolated(env):
    a, b = sign_in(env), sign_in(env)
    say(a, "Build me a habit tracker")
    env.drain()
    assert b.get("/api/chat/state").json() == {"messages": [], "build": None, "projects": []}
    say(b, "OPEN 1")
    say(b, "STATUS")
    assert all("/download/" not in t and "Lift Log" not in t for t in genesis_texts(b))


def test_resent_message_is_idempotent(env):
    c = sign_in(env)
    cid = uuid.uuid4().hex
    assert say(c, "Build me a habit tracker", cid).json()["outcome"] == "queued_new"
    assert say(c, "Build me a habit tracker", cid).json()["outcome"] == "duplicate"
    assert len(env.store.all("SELECT * FROM jobs")) == 1


def test_message_validation(env):
    c = sign_in(env)
    assert say(c, "").status_code == 422
    assert say(c, "x" * 1601).status_code == 422
    assert say(c, "hello", client_id="not-hex").status_code == 422
    assert c.post("/api/chat/messages", json={"text": "hi", "client_id": uuid.uuid4().hex}).status_code == 403


def test_web_address_cannot_be_spoofed_through_the_webhook(env):
    TestClient(env.app).post("/api/chat/session", json={"local": True}, headers=H)  # the web owner exists
    r = env.text("web:" + "0" * 32, "Build me a habit tracker")
    assert r.status_code == 400
    assert env.store.all("SELECT * FROM jobs") == []
