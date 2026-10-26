"""askweave.xyz accounts: sign-up, WhatsApp linking, <username>.<domain>/<project> hosting, the dashboard API,
delete/restore, takedowns and abuse reports. Gemini, Gradle and the weave bridge are faked as in test_unified."""

import pytest
from fastapi.testclient import TestClient
from test_sms import OWNER
from test_unified import UEnv
from test_website import PAGE, ok

from genesis.site.names import clean_call_sign, username_problem
from genesis.web.validate import public_problem

MEMBER = "+919876500002"
OTHER = "+919876500003"
H = {"X-Weave": "1"}


@pytest.fixture
def acc(settings, tmp_path):
    settings.website_deploy = "weave"
    settings.sites_domain = "weave.xyz"
    settings.sms_signup = "account"
    env = UEnv(settings, tmp_path, results=[ok(), ok(html=PAGE.replace("Brew Haven", "Brew Haven v2")), ok(), ok()])
    env.web_client = TestClient(env.app, base_url="https://app.weave.xyz")
    return env


def signup(env, username="sam", client=None, **kw):
    client = client or env.web_client
    data = {"username": username, "call_sign": "Sam", "email": f"{username}@example.com", "password": "correct horse",
            "terms": True, **kw}
    return client.post("/api/account/signup", json=data, headers=H)


def link(env, phone=MEMBER, client=None):
    code = (client or env.web_client).post("/api/account/phone-code", headers=H).json()["code"]
    env.text(phone, f"LINK {code}")
    return code


def site(env, path, host="sam.weave.xyz"):
    return env.client.get(path, headers={"host": host}, follow_redirects=False)


# ---------- names ----------
@pytest.mark.parametrize("name", ["admin", "www", "paypal", "paypa1", "pay-pal", "rnetflix", "g00gle", "sbi-help",
                                  "fuckit", "ab", "-sam", "sam-", "Sam", "sam--x", "a" * 21])
def test_bad_usernames_are_refused(name):
    assert username_problem(name)


@pytest.mark.parametrize("name", ["sam", "bakery-by-ana", "sbiswas", "jai2010"])
def test_ordinary_usernames_are_fine(name):
    assert username_problem(name) == ""


def test_call_signs():
    assert clean_call_sign("  Jai  K ") == "Jai K"
    assert clean_call_sign("visit http://x.co") == "" and clean_call_sign("<b>") == ""


# ---------- sign-up and sign-in ----------
def test_unknown_number_gets_the_sign_up_link_once(acc):
    acc.text(MEMBER, "build me a website for my bakery")
    assert "https://genesis.example.test/start" in acc.last(MEMBER) and acc.user(MEMBER) is None
    acc.text(MEMBER, "please build it")
    assert len(acc.all(MEMBER)) == 1  # at most once a day...
    acc.text(MEMBER, "hi")
    assert len(acc.all(MEMBER)) == 2  # ...unless they say hi


def test_signup_rules(acc):
    assert signup(acc, "admin").status_code == 400
    assert signup(acc, "paypa1").status_code == 400
    assert signup(acc, terms=False).status_code == 400
    assert signup(acc, password="short").status_code == 400
    assert acc.web_client.post("/api/account/signup", json={"username": "sam"}).status_code == 403  # no X-Weave
    r = signup(acc)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Domain" not in cookie  # host-only: never sent to customer subdomains
    me = acc.web_client.get("/api/account/me").json()
    assert me["username"] == "sam" and me["address"] == "https://sam.weave.xyz/" and me["phone"] is None
    other = TestClient(acc.app, base_url="https://app.weave.xyz")
    assert signup(acc, "sam", client=other, email="x@example.com").status_code == 400  # name taken
    assert signup(acc, "sam2", client=other, email="sam@example.com").status_code == 400  # email taken
    assert acc.web_client.get("/api/account/username?name=sam").json()["ok"] is False


def test_login_and_logout(acc):
    signup(acc)
    acc.web_client.post("/api/account/logout", headers=H)
    assert acc.web_client.get("/api/account/me").status_code == 401
    bad = acc.web_client.post("/api/account/login", json={"email": "sam@example.com", "password": "nope"}, headers=H)
    assert bad.status_code == 401
    good = acc.web_client.post("/api/account/login", json={"email": "sam@example.com", "password": "correct horse"},
                               headers=H)
    assert good.status_code == 200 and acc.web_client.get("/api/account/me").status_code == 200


def test_pages_and_front_door(acc):
    for path in ("/start", "/dashboard", "/terms", "/privacy", "/report", "/site/site.css", "/site/dashboard.js"):
        r = acc.web_client.get(path)
        assert r.status_code == 200, path
    assert "script-src 'self'" in acc.web_client.get("/start").headers["content-security-policy"]
    assert acc.web_client.get("/", follow_redirects=False).headers["location"] == "/start"


# ---------- WhatsApp linking ----------
def test_link_from_whatsapp_then_build_goes_live_under_the_username(acc):
    signup(acc)
    link(acc)
    assert "You're in, Sam!" in acc.last(MEMBER) and "sam.weave.xyz" in acc.last(MEMBER)
    assert acc.user(MEMBER)["account_id"]
    assert acc.web_client.get("/api/account/me").json()["phone"]
    acc.text(MEMBER, "Create a premium website for my coffee brand")
    acc.drain()
    assert "https://sam.weave.xyz/brew-haven/" in acc.last(MEMBER)
    assert site(acc, "/brew-haven").status_code == 308
    page = site(acc, "/brew-haven/")
    assert page.status_code == 200 and "Brew Haven" in page.text
    assert page.headers["content-security-policy"].startswith("sandbox")
    home = site(acc, "/")
    assert home.status_code == 200 and 'href="/brew-haven/"' in home.text
    assert site(acc, "/brew-haven/../../x").status_code == 404
    me = acc.web_client.get("/api/account/me").json()
    assert me["projects"][0]["url"] == "https://sam.weave.xyz/brew-haven/" and me["usage"]["today"]["builds"] == 1
    files = acc.web_client.get(f"/api/account/projects/{me['projects'][0]['id']}/files").json()
    assert [f["name"] for f in files["files"]] == ["index.html"] and "/preview/" in files["preview"]
    dl = acc.web_client.get(f"/api/account/projects/{me['projects'][0]['id']}/files/index.html")
    assert dl.status_code == 200 and "attachment" in dl.headers["content-disposition"]


def test_greeting_and_call_me(acc):
    signup(acc)
    link(acc)
    acc.text(MEMBER, "hi")
    assert acc.last(MEMBER).startswith("Hey Sam!")
    acc.text(MEMBER, "call me Captain")
    assert "Captain" in acc.last(MEMBER)
    assert acc.web_client.get("/api/account/me").json()["call_sign"] == "Captain"


def test_one_number_per_account_and_changing_number(acc):
    signup(acc)
    link(acc)
    acc.text(MEMBER, "Create a premium website for my coffee brand")
    acc.drain()
    other = TestClient(acc.app, base_url="https://app.weave.xyz")
    signup(acc, "ana", client=other)
    link(acc, MEMBER, client=other)
    assert "different Weave account" in acc.last(MEMBER)
    link(acc, OTHER)  # sam moves to a new number
    assert "projects came with you" in acc.last(OTHER)
    assert acc.user(OTHER)["account_id"] and len(acc.projects(OTHER)) == 1


def test_users_without_an_account_are_sent_to_sign_up(acc):
    code = acc.store.create_link_code("member")  # an old-style member made before accounts
    acc.settings.sms_signup = "link"
    acc.text(MEMBER, f"LINK {code}")
    acc.settings.sms_signup = "account"
    acc.text(MEMBER, "hi there")
    assert "/start" in acc.last(MEMBER) and acc.jobs(MEMBER) == []


# ---------- delete, restore, limits ----------
def test_delete_needs_yes_and_can_be_restored(acc):
    signup(acc)
    link(acc)
    acc.text(MEMBER, "Create a premium website for my coffee brand")
    acc.drain()
    acc.text(MEMBER, "DELETE 1")
    assert "Reply YES" in acc.last(MEMBER)
    acc.text(MEMBER, "no")
    assert "kept" in acc.last(MEMBER) and site(acc, "/brew-haven/").status_code == 200
    acc.text(MEMBER, "DELETE 1")
    acc.text(MEMBER, "yes")
    assert "Deleted" in acc.last(MEMBER) and site(acc, "/brew-haven/").status_code == 404
    me = acc.web_client.get("/api/account/me").json()
    assert me["projects"] == [] and len(me["trash"]) == 1
    r = acc.web_client.post(f"/api/account/projects/{me['trash'][0]['id']}/restore", headers=H)
    assert r.status_code == 200 and site(acc, "/brew-haven/").status_code == 200


def test_project_limit(acc):
    acc.settings.max_projects = 1
    signup(acc)
    link(acc)
    acc.text(MEMBER, "Create a premium website for my coffee brand")
    acc.drain()
    acc.text(MEMBER, "new: Create a premium website for my tea shop")
    assert "Delete one first" in acc.last(MEMBER)


def test_phishing_shaped_pages_stay_private(acc, tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    (d / "index.html").write_text("<html><body><input name=email><input type=password></body></html>")
    assert "password" in public_problem(d)
    (d / "index.html").write_text("<html><body><input name=email><input name=pinCode><textarea></textarea></body></html>")
    assert public_problem(d) == ""


# ---------- abuse ----------
def test_report_and_takedown(acc):
    signup(acc)
    link(acc)
    acc.text(MEMBER, "Create a premium website for my coffee brand")
    acc.drain()
    anon = TestClient(acc.app, base_url="https://app.weave.xyz")
    r = anon.post("/api/report", json={"site": "https://sam.weave.xyz/brew-haven/", "reason": "phishing"}, headers=H)
    assert r.status_code == 200
    acc.text(OWNER, "hi")  # the owner exists once they've texted
    anon.post("/api/report", json={"site": "https://sam.weave.xyz/brew-haven/", "reason": "phishing"}, headers=H)
    assert "Abuse report" in acc.last(OWNER)
    acc.text(OWNER, "TAKEDOWN sam")
    assert "Suspended sam" in acc.last(OWNER)
    assert site(acc, "/brew-haven/").status_code == 451
    assert acc.web_client.get("/api/account/me").status_code == 401  # signed out
    before = len(acc.all(MEMBER))
    acc.text(MEMBER, "Create a website for my bakery")
    assert len(acc.all(MEMBER)) == before and len(acc.jobs(MEMBER)) == 1
    acc.text(OWNER, "REINSTATE sam")
    assert site(acc, "/brew-haven/").status_code == 200


def test_delete_account_retires_the_username(acc):
    signup(acc)
    link(acc)
    assert acc.web_client.post("/api/account/delete", json={"confirm": "wrong"}, headers=H).status_code == 400
    assert acc.web_client.post("/api/account/delete", json={"confirm": "sam"}, headers=H).status_code == 200
    assert acc.web_client.get("/api/account/me").status_code == 401
    other = TestClient(acc.app, base_url="https://app.weave.xyz")
    assert signup(acc, "sam", client=other).status_code == 400
    acc.text(MEMBER, "hi")
    assert "/start" in acc.last(MEMBER)  # the number is free to join again


def test_export(acc):
    signup(acc)
    link(acc)
    r = acc.web_client.get("/api/account/export")
    assert r.status_code == 200 and r.json()["account"]["username"] == "sam" and r.json()["phone"] == MEMBER


def test_housekeeping_backs_up_and_purges(acc):
    out = acc.sms.housekeeping()
    assert out["backup"].is_file() and out["purged"] == 0
