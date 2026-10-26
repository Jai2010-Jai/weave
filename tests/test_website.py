"""Website builder: weave bridge contract, pipeline, validation, rollback and Netlify publishing.
The bridge test runs the real bridge.mjs under Node against a fake weave engine (no network); the pipeline tests
use a fake runner. No test calls Gemini or Netlify."""

import json
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from genesis.core.schemas import BuildRequest
from genesis.web import deploy
from genesis.web.builder import BRIDGE, WebsiteBuilder, node_runner
from genesis.web.validate import WebsiteValidationError, validate_page, validate_site

PAGE = ("<!DOCTYPE html><html><head><script src=\"https://cdn.tailwindcss.com\"></script></head>"
        "<body><h1>Brew Haven</h1></body></html>")


def engine_dir(tmp_path: Path) -> Path:
    d = tmp_path / "weave"
    (d / "src/portfolio").mkdir(parents=True)
    (d / "src/portfolio/bespokeHtmlGenerator.js").write_text("// placeholder for availability checks\n")
    return d


@pytest.fixture
def web_settings(settings, tmp_path):
    settings.website_engine_dir = str(engine_dir(tmp_path))
    settings.node_bin = shutil.which("node") or str(tmp_path / "node")
    Path(settings.node_bin).touch(exist_ok=True) if not shutil.which("node") else None
    settings.website_builder = True
    return settings


class FakeRunner:
    def __init__(self, results):
        self.results = list(results)
        self.requests = []
        self.envs = []

    def __call__(self, request, env, cwd, timeout, cancel, log_path):
        self.requests.append(request)
        self.envs.append(env)
        r = self.results.pop(0)
        if callable(r):
            return r(request)
        if r.get("ok"):
            out = Path(request["out_dir"])
            for page in r.get("pages", [{"file": "index.html"}]):
                (out / page["file"]).write_text(r.get("html", PAGE), encoding="utf-8")
        return r


def ok(html=PAGE, **kw):
    return {"ok": True, "brand_name": "Brew Haven", "research": "ai", "pages": [{"file": "index.html", "option": 1}],
            "html": html, "calls": [{"phase": "research", "ok": True}, {"phase": "html-1", "ok": True}], **kw}


def build(builder, prompt="A premium website for my coffee brand", base=None, change=None):
    job = builder.create_job(BuildRequest(prompt=prompt), base_job_id=base, change_request=change)
    return builder.run_existing(job["id"])


# ---------- pipeline ----------
def test_generate_success_records_files_and_ai_calls(web_settings):
    runner = FakeRunner([ok()])
    b = WebsiteBuilder(web_settings, runner=runner)
    events = []
    job = b.create_job(BuildRequest(prompt="A premium website for my coffee brand"))
    result = b.run_existing(job["id"], listener=lambda j, ev: events.append(ev))
    assert result["status"] == "succeeded" and result["kind"] == "website"
    assert result["app_name"] == "Brew Haven"
    assert result["site"]["files"][0]["file"] == "index.html"
    assert [c["kind"] for c in result["ai_calls"]] == ["research", "html-1"]
    assert events == ["generating", "validating", "verifying", "succeeded"]
    assert b.site_ok(result["id"])
    assert runner.requests[0]["op"] == "generate" and runner.requests[0]["variants"] == 1
    assert not (b.store.path(result["id"]) / "sandbox").exists()


def test_bridge_env_holds_only_the_gemini_key(web_settings):
    web_settings.netlify_auth_token = "nfp_secret_token"
    runner = FakeRunner([ok()])
    build(WebsiteBuilder(web_settings, runner=runner))
    env = runner.envs[0]
    assert env["GEMINI_API_KEY"] == web_settings.gemini_api_key
    assert env["AGY_BIN"] == "/nonexistent/agy"
    joined = json.dumps(env)
    for secret in ("nfp_secret_token", web_settings.twilio_auth_token):
        assert secret not in joined
    assert set(env) == {"PATH", "HOME", "TMPDIR", "WEAVE_DATA_DIR", "AGY_BIN", "GEMINI_API_KEY", "GEMINI_MODEL",
                        "GEMINI_FALLBACK_MODELS", "NODE_OPTIONS", "WEBSITE_DEADLINE_MS",
                        "GENESIS_WEBSITE_THINKING", "GEMINI_HEALTH_FILE", "GENESIS_AI_PROVIDER", "GLM_BASE_URL",
                        "GLM_API_KEY"}


def test_engine_failure_fails_the_build(web_settings):
    b = WebsiteBuilder(web_settings, runner=FakeRunner([{"ok": False, "error": "Gemini API error (429): quota"}]))
    result = build(b)
    assert result["status"] == "failed"
    assert "429" in result["error"] and result["site"] is None
    assert not b.site_ok(result["id"])


@pytest.mark.parametrize("bad", [
    PAGE.replace("https://cdn.tailwindcss.com", "https://evil.example/x.js"),
    PAGE.replace("<h1>", "<iframe src='https://x.test'></iframe><h1>"),
    "just some text, not a page",
])
def test_unsafe_or_broken_output_is_rejected(web_settings, bad):
    result = build(WebsiteBuilder(web_settings, runner=FakeRunner([ok(html=bad)])))
    assert result["status"] == "failed"
    assert "failed validation" in result["error"]


def test_unavailable_engine_reports_reason(web_settings):
    web_settings.website_engine_dir = ""
    runner = FakeRunner([ok()])
    result = build(WebsiteBuilder(web_settings, runner=runner))
    assert result["status"] == "failed"
    assert "GENESIS_WEBSITE_ENGINE_DIR" in result["error"]
    assert runner.requests == []


def test_website_needs_gemini_api_backend(web_settings):
    web_settings.ai_backend = "antigravity"
    assert "GENESIS_AI_BACKEND=api" in web_settings.website_problem()


def test_change_creates_new_version_and_failure_keeps_last_good(web_settings):
    changed = PAGE.replace("Brew Haven", "Brew Haven Dark")
    runner = FakeRunner([ok(), ok(html=changed), {"ok": False, "error": "The AI did not apply the change"}])
    b = WebsiteBuilder(web_settings, runner=runner)
    v1 = build(b)
    v2 = build(b, prompt="A premium website for my coffee brand", base=v1["id"], change="make it dark")
    assert v2["status"] == "succeeded" and v2["base_job_id"] == v1["id"]
    assert runner.requests[1]["op"] == "modify" and runner.requests[1]["feedback"] == "make it dark"
    assert runner.requests[1]["base_dir"].endswith(f"{v1['id']}/site")
    v3 = build(b, prompt="A premium website for my coffee brand", base=v2["id"], change="add pricing")
    assert v3["status"] == "failed"
    # Earlier versions are untouched.
    assert (b.site_dir(v1["id"]) / "index.html").read_text() == PAGE
    assert (b.site_dir(v2["id"]) / "index.html").read_text() == changed
    assert b.site_ok(v2["id"])


def test_change_requires_successful_base(web_settings):
    b = WebsiteBuilder(web_settings, runner=FakeRunner([{"ok": False, "error": "x"}]))
    failed = build(b)
    with pytest.raises(ValueError):
        b.create_job(BuildRequest(prompt="A premium website for my coffee brand"), base_job_id=failed["id"])


def test_cancel_before_start(web_settings):
    runner = FakeRunner([ok()])
    b = WebsiteBuilder(web_settings, runner=runner)
    job = b.create_job(BuildRequest(prompt="A premium website for my coffee brand"))
    b._cancel[job["id"]] = threading.Event()
    b.cancel(job["id"])
    result = b.run_existing(job["id"])
    assert result["status"] == "failed" and result["cancelled"] is True
    assert runner.requests == []


# ---------- validation ----------
def test_validate_allows_tailwind_and_relative_anchors():
    validate_page(PAGE.replace("<body>", "<body><form action='#'><a href='#top'>x</a></form>"))


@pytest.mark.parametrize("html", [
    PAGE.replace("https://cdn.tailwindcss.com", "http://cdn.tailwindcss.com"),
    PAGE.replace("<head>", "<head><base href='https://x.test/'>"),
    PAGE.replace("<head>", "<head><meta http-equiv='refresh' content='0;url=https://x.test'>"),
    PAGE.replace("<body>", "<body><form action='https://collect.example/'></form>"),
])
def test_validate_rejects(html):
    with pytest.raises(WebsiteValidationError):
        validate_page(html)


# ---------- real bridge.mjs against a fake weave engine ----------
FAKE_GEMINI = """
export class GeminiClient {
  constructor() { this.apiKey = process.env.GEMINI_API_KEY || ''; this.model = process.env.GEMINI_MODEL; }
  async generateContent(prompt, system, config) {
    const mode = process.env.FAKE_MODE;
    if (mode === 'busy' && this.model === 'fake-model') throw new Error('Gemini API error (503): high demand');
    if (mode === 'stall' && this.model === 'fake-model') await new Promise((r) => setTimeout(r, 8000));
    if (mode === 'down') throw new Error('Gemini API error (503): overloaded key=AIzaSyFAKEFAKEFAKEFAKEFAKEFAKE');
    if (mode === 'nothink' && config?.thinkingConfig) throw new Error('Gemini API error (400): thinking_level is not supported');
    if (mode === 'ok' && !config?.thinkingConfig) throw new Error('Gemini API error (400): expected a thinking setting');
    if (system.includes('Design Director') && mode === 'badjson') return { text: '{"topicAnalysis": {"brandName": "X",}', raw: {} };
    if (system.includes('Design Director') && !(config?.responseMimeType === 'application/json'
        && config?.responseJsonSchema?.properties?.inspirations?.minItems === 3)) return { text: 'not json', raw: {} };
    if (system.includes('Design Director')) return { text: JSON.stringify({ topicAnalysis: { brandName: 'Brew Haven' },
      inspirations: [{ name: 'Roast', sourceInspiration: 'Cafe' }, { name: 'B' }, { name: 'C' }] }), raw: {} };
    if (system.includes('page structure')) return { text: JSON.stringify({ pages: [{ slug: 'About', title: 'About', purpose: 'story' },
      { slug: 'index', title: 'Home', purpose: 'dup' }, { slug: '../evil', title: 'Evil', purpose: 'x' }, { slug: 'contact', title: 'Contact', purpose: 'reach us' }] }), raw: {} };
    if (system.includes('matching page')) return { text: '```html\\n<!DOCTYPE html><html><body><nav><a href="index.html">Home</a><a href="about.html">About</a><a href="contact.html">Contact</a></nav>page</body></html>\\n```', raw: {} };
    if (mode === 'nohtml') return { text: 'Sorry, I cannot.', raw: {} };
    if (system.includes('critique')) return { text: mode === 'same' ? CURRENT : '<!DOCTYPE html><html><body>changed</body></html>', raw: {} };
    const budget = system.includes('LENGTH BUDGET') ? ' budgeted' : '';
    return { text: '```html\\n<!DOCTYPE html><html><body>model page' + budget + '</body></html>\\n```', raw: { usageMetadata: { promptTokenCount: 5 } } };
  }
  async generateViaAgy() { return { text: '<!DOCTYPE html><html><body>agy</body></html>' }; }
}
let CURRENT = '';
export function setCurrent(c) { CURRENT = c; }
export const geminiClient = new GeminiClient();
"""
FAKE_RESEARCH = """
import dotenv from 'dotenv';
import { geminiClient } from '../gemini-client.js';
dotenv.config({ path: '/should/not/be/read' });
export async function researchDesignInspirations(prompt) {
  try { return JSON.parse((await geminiClient.generateContent(prompt, 'Design Director')).text); }
  catch { return { topicAnalysis: { brandName: 'Fallback' }, inspirations: [{ name: 'canned' }] }; }
}
"""
FAKE_BESPOKE = """
import { geminiClient, setCurrent } from '../gemini-client.js';
const CANNED = '<!DOCTYPE html><html><body>canned template - Designed with Google Stitch MCP</body></html>';
export async function generateBespokeHtml({ prompt }) {
  try {
    let html = (await geminiClient.generateContent(prompt, 'Legendary')).text.trim();
    html = html.replace(/^```[a-z]*\\s*/i, '').replace(/\\s*```$/i, '');
    if (html.includes('<html') && html.includes('</html>')) return html;
  } catch {}
  return CANNED;
}
export async function redesignBespokeHtml({ currentHtml, feedbackText }) {
  setCurrent(currentHtml);
  try {
    const html = (await geminiClient.generateContent(currentHtml, 'critique ' + feedbackText)).text.trim();
    if (html.includes('<html') && html.includes('</html>')) return html;
  } catch {}
  return currentHtml;
}
"""


@pytest.fixture
def fake_engine(tmp_path):
    d = tmp_path / "fake-weave"
    (d / "src/portfolio").mkdir(parents=True)
    (d / "src/gemini-client.js").write_text(FAKE_GEMINI)
    (d / "src/portfolio/designInspirationResearcher.js").write_text(FAKE_RESEARCH)
    (d / "src/portfolio/bespokeHtmlGenerator.js").write_text(FAKE_BESPOKE)
    return d


needs_node = pytest.mark.skipif(not shutil.which("node"), reason="Node.js is not installed")


def run_bridge(settings, tmp_path, request, mode="ok", key="AIzaSyFAKEFAKEFAKEFAKEFAKEFAKE", extra_env=None):
    settings.node_bin = shutil.which("node")
    out = tmp_path / f"out-{mode}"
    out.mkdir(exist_ok=True)
    sandbox = tmp_path / f"sb-{mode}"
    sandbox.mkdir(exist_ok=True)
    env = {"PATH": f"{Path(settings.node_bin).parent}:/usr/bin:/bin", "HOME": str(sandbox), "GEMINI_API_KEY": key,
           "GEMINI_MODEL": "fake-model", "FAKE_MODE": mode, "WEAVE_DATA_DIR": str(sandbox),
           "GENESIS_RESEARCH_RETRY_MS": "0", "GEMINI_FALLBACK_MODELS": os.environ.get("GEMINI_FALLBACK_MODELS", ""),
           **(extra_env or {})}
    log = tmp_path / "bridge.log"
    result = node_runner(settings)({**request, "out_dir": str(out)}, env, sandbox, 60, threading.Event(), log)
    return result, out


@needs_node
def test_bridge_generates_page_from_model(settings, tmp_path, fake_engine):
    result, out = run_bridge(settings, tmp_path, {"op": "generate", "engine_dir": str(fake_engine), "prompt": "coffee brand site"})
    assert result["ok"] is True, result
    assert result["brand_name"] == "Brew Haven" and result["research"] == "ai"
    assert (out / "index.html").read_text() == "<!DOCTYPE html><html><body>model page budgeted</body></html>"
    assert [c["phase"] for c in result["calls"]] == ["research", "html-1"]
    assert result["calls"][1]["input_tokens"] == 5


@needs_node
def test_bridge_adds_extra_pages_and_links_them_from_home(settings, tmp_path, fake_engine):
    result, out = run_bridge(settings, tmp_path, {"op": "generate", "engine_dir": str(fake_engine), "prompt": "coffee",
                                                  "extra_pages": 3})
    assert result["ok"] is True, result
    # "index" is refused (reserved) and "../evil" can only ever become a plain file inside the site.
    assert sorted(p.name for p in out.iterdir()) == ["about.html", "contact.html", "evil.html", "index.html"]
    assert [p["file"] for p in result["pages"]] == ["index.html", "about.html", "evil.html", "contact.html"]
    assert not (out.parent / "evil.html").exists()
    assert 'href="contact.html"' in (out / "index.html").read_text()  # nav added to the home page
    assert validate_site(out)  # extra page names pass validation


@needs_node
def test_bridge_change_keeps_extra_pages(settings, tmp_path, fake_engine):
    base = tmp_path / "base"
    base.mkdir()
    (base / "index.html").write_text("<!DOCTYPE html><html><body>old</body></html>")
    (base / "about.html").write_text("<!DOCTYPE html><html><body>about</body></html>")
    req = {"op": "modify", "engine_dir": str(fake_engine), "base_dir": str(base), "feedback": "make it dark"}
    result, out = run_bridge(settings, tmp_path, req)
    assert result["ok"] is True and (out / "about.html").read_text().endswith("about</body></html>")


@needs_node
def test_bridge_switches_model_when_gemini_is_overloaded(settings, tmp_path, fake_engine):
    import os
    os.environ["GEMINI_FALLBACK_MODELS"] = "backup-model"
    try:
        result, out = run_bridge(settings, tmp_path, {"op": "generate", "engine_dir": str(fake_engine), "prompt": "coffee"},
                                 mode="busy")
    finally:
        del os.environ["GEMINI_FALLBACK_MODELS"]
    assert result["ok"] is True, result
    assert (out / "index.html").exists()
    assert [(c["model"], c["ok"]) for c in result["calls"] if c["phase"] == "research"] == [
        ("fake-model", False), ("fake-model", False), ("backup-model", True)]


@needs_node
def test_bridge_reports_canned_fallback_as_failure_and_redacts(settings, tmp_path, fake_engine):
    result, out = run_bridge(settings, tmp_path, {"op": "generate", "engine_dir": str(fake_engine), "prompt": "coffee"}, mode="down")
    assert result["ok"] is False
    assert "could not design" in result["error"]  # research fails first now; no canned page either way
    assert not (out / "index.html").exists()
    assert "AIzaSy" not in json.dumps(result)
    result, _ = run_bridge(settings, tmp_path, {"op": "generate", "engine_dir": str(fake_engine), "prompt": "coffee"}, mode="nohtml")
    assert result["ok"] is False


@needs_node
def test_bridge_detects_research_fallback_after_a_successful_call(settings, tmp_path, fake_engine):
    result, out = run_bridge(settings, tmp_path, {"op": "generate", "engine_dir": str(fake_engine), "prompt": "coffee"},
                             mode="badjson")
    # weave's canned directions would name the site after the prompt's first words: retry, then fail honestly.
    assert result["ok"] is False and "could not design" in result["error"]
    assert [c["phase"] for c in result["calls"]] == ["research", "research", "research"]
    assert not (out / "index.html").exists()


@needs_node
def test_bridge_never_uses_agy_without_a_key(settings, tmp_path, fake_engine):
    result, out = run_bridge(settings, tmp_path, {"op": "generate", "engine_dir": str(fake_engine), "prompt": "coffee"}, key="")
    assert result["ok"] is False
    assert all(not c["ok"] for c in result["calls"])
    assert not (out / "index.html").exists()


@needs_node
def test_bridge_modify_detects_unchanged_page(settings, tmp_path, fake_engine):
    base = tmp_path / "base"
    base.mkdir()
    (base / "index.html").write_text("<!DOCTYPE html><html><body>old</body></html>")
    req = {"op": "modify", "engine_dir": str(fake_engine), "base_dir": str(base), "feedback": "make it dark"}
    result, out = run_bridge(settings, tmp_path, req)
    assert result["ok"] is True and (out / "index.html").read_text() == "<!DOCTYPE html><html><body>changed</body></html>"
    result, _ = run_bridge(settings, tmp_path, req, mode="same")
    assert result["ok"] is False and "did not apply" in result["error"]


@needs_node
def test_bridge_file_is_the_one_the_builder_runs():
    assert BRIDGE.name == "bridge.mjs" and BRIDGE.is_file()


# ---------- Netlify publishing ----------
class FakeNetlify:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, body, headers):
        self.calls.append((method, url, headers))
        return self.responses.pop(0)


def site(tmp_path):
    d = tmp_path / "site"
    d.mkdir()
    (d / "index.html").write_text(PAGE)
    return d


def test_publish_creates_site_and_returns_https_url(tmp_path):
    http = FakeNetlify([(201, {"id": "site-1"}), (200, {"id": "dep-1", "state": "uploading"}),
                        (200, {"id": "dep-1", "state": "ready", "ssl_url": "https://genesis-brew.netlify.app"})])
    out = deploy.publish("tok", site(tmp_path), brand="Brew Haven", site_id=None, http=http, poll_s=0)
    assert out == {"site_id": "site-1", "url": "https://genesis-brew.netlify.app", "deploy_id": "dep-1"}
    assert [c[0] for c in http.calls] == ["POST", "POST", "GET"]
    assert all(c[2]["Authorization"] == "Bearer tok" for c in http.calls)
    assert http.calls[1][2]["Content-Type"] == "application/zip"


def test_publish_reuses_existing_site(tmp_path):
    http = FakeNetlify([(200, {"id": "dep-2", "state": "ready", "ssl_url": "https://x.netlify.app"})])
    out = deploy.publish("tok", site(tmp_path), brand="B", site_id="site-1", http=http)
    assert out["site_id"] == "site-1" and http.calls[0][1].endswith("/sites/site-1/deploys")


def test_publish_without_token_never_calls_netlify(tmp_path):
    http = FakeNetlify([])
    with pytest.raises(deploy.PublishError, match="NETLIFY_AUTH_TOKEN"):
        deploy.publish("", site(tmp_path), brand="B", site_id=None, http=http)
    assert http.calls == []


def test_publish_auth_error_does_not_leak_token(tmp_path):
    http = FakeNetlify([(401, {"message": "Access Denied"})])
    with pytest.raises(deploy.PublishError) as e:
        deploy.publish("nfp_very_secret", site(tmp_path), brand="B", site_id=None, http=http)
    assert "nfp_very_secret" not in str(e.value) and "rejected the token" in str(e.value)


def test_name_comes_from_page_title_when_research_fell_back(web_settings):
    page = PAGE.replace("<head>", "<head><title>Ember &amp; Oak — Specialty Coffee | Bangalore</title>")
    result = build(WebsiteBuilder(web_settings, runner=FakeRunner([
        ok(html=page, research="fallback", brand_name="A Premium Website")])))
    assert result["app_name"] == "Ember & Oak" and result["research"] == "fallback"
    result = build(WebsiteBuilder(web_settings, runner=FakeRunner([ok(html=page)])))
    assert result["app_name"] == "Brew Haven"  # research succeeded: its brand name wins


@needs_node
def test_bridge_drops_thinking_setting_a_model_rejects(settings, tmp_path, fake_engine):
    result, out = run_bridge(settings, tmp_path, {"op": "generate", "engine_dir": str(fake_engine), "prompt": "coffee"},
                             mode="nothink")
    assert result["ok"] is True, result
    assert [c["ok"] for c in result["calls"] if c["phase"] == "research"] == [False, True]  # one 400, then without
    assert [c["ok"] for c in result["calls"] if c["phase"] == "html-1"] == [True]  # remembered for the next call


@needs_node
def test_bridge_shares_model_health_between_builds(settings, tmp_path, fake_engine):
    health = tmp_path / "gemini-health.json"
    env = {"GEMINI_FALLBACK_MODELS": "backup-model", "GEMINI_HEALTH_FILE": str(health)}
    req = {"op": "generate", "engine_dir": str(fake_engine), "prompt": "coffee"}
    first, _ = run_bridge(settings, tmp_path, req, mode="busy", extra_env=env)
    assert first["ok"] is True and "fake-model" in json.loads(health.read_text())
    second, _ = run_bridge(settings, tmp_path, req, mode="busy", extra_env=env)
    assert second["ok"] is True
    assert [c["model"] for c in second["calls"]][:2] == ["backup-model", "backup-model"]  # busy model skipped


@needs_node
def test_bridge_hedges_a_stalled_model_onto_the_next_one(settings, tmp_path, fake_engine):
    env = {"GEMINI_FALLBACK_MODELS": "backup-model", "GENESIS_HEDGE_MS": "200"}
    req = {"op": "generate", "engine_dir": str(fake_engine), "prompt": "coffee"}
    started = time.monotonic()
    result, out = run_bridge(settings, tmp_path, req, mode="stall", extra_env=env)
    assert result["ok"] is True and time.monotonic() - started < 6  # never waited out the 8 s stall
    winners = [c for c in result["calls"] if c["ok"] and ":" not in c["phase"]]
    assert {c["model"] for c in winners} == {"backup-model"}
    assert "budgeted" in (out / "index.html").read_text()
