import time

import pytest
from conftest import FakeBuilder, FakeGemini, manifest
from fastapi.testclient import TestClient

from genesis.core.jobs import Orchestrator
from genesis.server import create_app


@pytest.fixture
def client(settings):
    orch = Orchestrator(settings, FakeGemini(manifest()), builder=FakeBuilder(settings))
    app = create_app(settings, orchestrator=orch)
    return TestClient(app)


def wait_done(client, job_id, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/builds/{job_id}").json()
        if j["status"] in ("succeeded", "failed"):
            return j
        time.sleep(0.05)
    raise AssertionError("build did not finish")


def test_dashboard_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Weave" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]


def test_full_flow_and_download(client):
    r = client.post("/api/builds", json={"prompt": "A lifting tracker with history", "app_name": "Lift Log"})
    assert r.status_code == 202
    job = wait_done(client, r.json()["id"])
    assert job["status"] == "succeeded"
    assert "workspace" not in str(job)  # no filesystem paths leak
    apk = client.get(f"/api/builds/{job['id']}/apk")
    assert apk.status_code == 200
    assert apk.headers["content-type"] == "application/vnd.android.package-archive"
    assert client.get(f"/api/builds/{job['id']}/log").status_code == 200
    assert [j["id"] for j in client.get("/api/builds").json()] == [job["id"]]


def test_input_validation(client):
    assert client.post("/api/builds", json={"prompt": "short"}).status_code == 422
    assert client.post("/api/builds", json={"prompt": "x" * 5000}).status_code == 422
    assert client.post("/api/builds", json={"prompt": "valid prompt here", "extra": 1}).status_code == 422
    assert client.post("/api/builds", content=b"not json", headers={"content-type": "application/json"}).status_code == 400
    big = {"prompt": "y" * 20000}
    assert client.post("/api/builds", json=big).status_code == 413


@pytest.mark.parametrize("bad", ["../../etc", "abc", "0" * 31 + "g", "%2e%2e"])
def test_bad_ids(client, bad):
    assert client.get(f"/api/builds/{bad}").status_code == 404
    assert client.get(f"/api/builds/{bad}/apk").status_code in (404, 405)


def test_download_blocked_before_success(client, settings):
    from genesis.core.schemas import BuildRequest

    job = client.app.state.orchestrator.store.create(BuildRequest(prompt="pending build please"))
    assert client.get(f"/api/builds/{job['id']}/apk").status_code == 409


def test_cross_origin_post_rejected(client):
    r = client.post("/api/builds", json={"prompt": "A lifting tracker with history"},
                    headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_non_local_host_rejected(client):
    assert client.get("/api/builds", headers={"host": "genesis.example.com"}).status_code == 401


def test_rate_limit(client, settings):
    settings.max_queued_builds = 50
    codes = [client.post("/api/builds", json={"prompt": f"A lifting tracker number {i}"}).status_code for i in range(8)]
    assert codes.count(202) == 6 and codes[-1] == 429


# ---------- phone app access ----------
def remote_client(client):
    """A TestClient whose requests look like they come from a phone on the LAN."""
    return TestClient(client.app, client=("192.168.1.50", 50000), base_url="http://192.168.1.10:8765")


def code_of(settings):
    return (settings.data_dir / "access_code").read_text().strip()


def test_remote_requires_access_code(client):
    rc = remote_client(client)
    assert rc.get("/api/builds").status_code == 401
    front = rc.get("/", follow_redirects=False)  # the public front door is the sign-up page, not the dashboard
    assert front.status_code == 303 and front.headers["location"] == "/start"
    assert rc.get("/api/builds", headers={"Authorization": "Bearer WRONG-CODE-1234-5678"}).status_code == 401


def test_remote_with_code_can_build_and_download(client, settings):
    rc = remote_client(client)
    h = {"Authorization": "Bearer " + code_of(settings).lower().replace("-", " ")}  # forgiving format
    r = rc.post("/api/builds", json={"prompt": "A lifting tracker with history"}, headers=h)
    assert r.status_code == 202
    job = wait_done(client, r.json()["id"])
    assert job["status"] == "succeeded" and job["primary_color"] == "#7C4DFF"
    assert rc.get(f"/api/builds/{job['id']}/apk", headers=h).status_code == 200


def test_pairing_only_local(client, settings):
    r = client.get("/api/pairing")
    assert r.status_code == 200 and r.json()["access_code"] == code_of(settings)
    rc = remote_client(client)
    h = {"Authorization": "Bearer " + code_of(settings)}
    assert rc.get("/api/pairing", headers=h).status_code == 403


def test_wrong_codes_are_rate_limited(client):
    rc = remote_client(client)
    codes = [rc.get("/api/builds", headers={"Authorization": f"Bearer BAD{i:013d}"}).status_code for i in range(12)]
    assert codes[:10] == [401] * 10 and codes[-1] == 429


def test_dns_rebinding_blocked(client):
    # Loopback client but foreign Host header and no code -> rejected.
    assert client.get("/api/builds", headers={"host": "attacker.example"}).status_code == 401
