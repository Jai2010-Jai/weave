"""Configuration and toolchain detection. Secrets are read from the environment only."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(lo, min(hi, value))


def _find_java_home() -> Path | None:
    """Locate a JDK 17+ (AGP 8.x requires 17)."""
    candidates: list[Path] = []
    env = os.environ.get("JAVA_HOME", "").strip()
    if env:
        candidates.append(Path(env))
    # macOS JVM paths
    for base in (Path.home() / "Library/Java/JavaVirtualMachines", Path("/Library/Java/JavaVirtualMachines")):
        if base.is_dir():
            candidates.extend(sorted((p / "Contents/Home" for p in base.iterdir()), reverse=True))
    candidates += [
        Path("/usr/local/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home"),
        Path("/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home"),
        Path("/Applications/Android Studio.app/Contents/jbr/Contents/Home"),
    ]
    # Windows JVM and Android Studio paths
    win_bases = [
        Path("C:/Program Files/Android/Android Studio/jbr"),
        Path("C:/Program Files (x86)/Android/Android Studio/jbr"),
        Path.home() / "AppData/Local/Android/Android Studio/jbr",
    ]
    for d in (Path("C:/Program Files/Java"), Path("C:/Program Files/Eclipse Adoptium"),
              Path("C:/Program Files/Microsoft"), Path("C:/Program Files/Amazon Corretto"),
              Path("C:/Program Files/Zulu"), Path.home() / "AppData/Local/Programs/Eclipse Adoptium"):
        if d.is_dir():
            try:
                win_bases.extend(sorted((p for p in d.iterdir() if p.is_dir()), reverse=True))
            except OSError:
                pass
    candidates.extend(win_bases)

    # Linux paths
    for base in (Path("/usr/lib/jvm"),):
        if base.is_dir():
            try:
                candidates.extend(sorted((p for p in base.iterdir() if p.is_dir()), reverse=True))
            except OSError:
                pass

    for home in candidates:
        major = java_major_version(home)
        if major is not None and major >= 17:
            return home
    return None


def java_major_version(home: Path) -> int | None:
    java_bin = home / "bin"
    java = java_bin / ("java.exe" if sys.platform == "win32" or not (java_bin / "java").is_file() else "java")
    if not java.is_file():
        if (java_bin / "java.exe").is_file():
            java = java_bin / "java.exe"
        elif (java_bin / "java").is_file():
            java = java_bin / "java"
        else:
            return None
    try:
        out = subprocess.run([str(java), "-version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r'version "(\d+)(?:\.(\d+))?', out.stderr + out.stdout)
    if not m:
        return None
    major = int(m.group(1))
    return int(m.group(2)) if major == 1 and m.group(2) else major


def _find_android_home() -> Path | None:
    for name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        v = os.environ.get(name, "").strip()
        if v and Path(v).is_dir():
            return Path(v)
    for default in (
        Path.home() / "AppData/Local/Android/Sdk",  # Windows
        Path.home() / "Library/Android/sdk",        # macOS
        Path.home() / "Android/Sdk",                # Linux
    ):
        if default.is_dir():
            return default
    return None


def find_cloudflared() -> str | None:
    found = shutil.which("cloudflared")
    if found:
        return found
    candidates = [
        Path("C:/Program Files (x86)/cloudflared/cloudflared.exe"),
        Path("C:/Program Files/cloudflared/cloudflared.exe"),
        Path.home() / "AppData/Local/Programs/cloudflared/cloudflared.exe",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def _str_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _choice_env(name: str, default: str, choices: set[str]) -> str:
    value = _str_env(name, default).lower()
    return value if value in choices else default


def _list_env(name: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


@dataclass
class Settings:
    root: Path = ROOT
    template_dir: Path = ROOT / "android_template"
    data_dir: Path = ROOT / "data"
    gemini_api_key: str = field(default_factory=lambda: os.environ.get("GEMINI_API_KEY", "").strip(), repr=False)
    gemini_model: str = field(default_factory=lambda: os.environ.get("GEMINI_MODEL", "auto").strip() or "auto")
    # gemini = Google's Gemini API; glm = Zhipu / Z.ai GLM models (GLM_API_KEY) for chat, apps and websites alike.
    ai_provider: str = field(default_factory=lambda: _choice_env("GENESIS_AI_PROVIDER", "gemini", {"gemini", "glm"}))
    glm_api_key: str = field(default_factory=lambda: _str_env("GLM_API_KEY"), repr=False)
    glm_base_url: str = field(default_factory=lambda: _str_env("GLM_BASE_URL") or "https://api.z.ai/api/paas/v4")
    glm_model: str = field(default_factory=lambda: _str_env("GLM_MODEL") or "glm-4.7-flash")
    # Asked when the main GLM model stays busy. Empty by default: only glm-4.7-flash is free (glm-4.5-flash answers
    # "insufficient balance" without a paid plan), and busy answers are retried on the same model instead.
    glm_fallback_models: str = field(default_factory=lambda: _str_env("GLM_FALLBACK_MODELS"))
    # api = Gemini API with GEMINI_API_KEY; antigravity = the Antigravity desktop app's agent (no API key).
    ai_backend: str = field(default_factory=lambda: _choice_env("GENESIS_AI_BACKEND", "api", {"api", "antigravity"}))
    antigravity_model: str = field(default_factory=lambda: _choice_env(
        "GENESIS_ANTIGRAVITY_MODEL", "app", {"app", "flash_lite", "flash", "pro"}))
    antigravity_project: str = field(default_factory=lambda: _str_env("GENESIS_ANTIGRAVITY_PROJECT", "outside-of-project")
                                     or "outside-of-project")
    antigravity_timeout: int = field(default_factory=lambda: _int_env("GENESIS_ANTIGRAVITY_TIMEOUT_SECONDS", 600, 60, 1800))
    java_home: Path | None = field(default_factory=_find_java_home)
    android_home: Path | None = field(default_factory=_find_android_home)
    build_timeout: int = field(default_factory=lambda: _int_env("GENESIS_BUILD_TIMEOUT_SECONDS", 900, 60, 3600))
    max_repairs: int = field(default_factory=lambda: _int_env("GENESIS_MAX_REPAIR_ATTEMPTS", 2, 0, 2))
    max_concurrent_builds: int = field(default_factory=lambda: _int_env("GENESIS_MAX_CONCURRENT_BUILDS", 1, 1, 4))
    max_queued_builds: int = field(default_factory=lambda: _int_env("GENESIS_MAX_QUEUED_BUILDS", 5, 1, 50))
    host: str = field(default_factory=lambda: os.environ.get("GENESIS_HOST", "127.0.0.1").strip() or "127.0.0.1")
    port: int = field(default_factory=lambda: _int_env("GENESIS_PORT", 8765, 1024, 65535))

    # ---- SMS (Twilio). Secrets are server-side only and never logged or returned by the API. ----
    sms_provider: str = field(default_factory=lambda: _choice_env("GENESIS_SMS_PROVIDER", "twilio", {"twilio", "log", "off"}))
    twilio_account_sid: str = field(default_factory=lambda: _str_env("TWILIO_ACCOUNT_SID"))
    twilio_auth_token: str = field(default_factory=lambda: _str_env("TWILIO_AUTH_TOKEN"), repr=False)
    twilio_phone_number: str = field(default_factory=lambda: _str_env("TWILIO_PHONE_NUMBER"))
    twilio_messaging_service_sid: str = field(default_factory=lambda: _str_env("TWILIO_MESSAGING_SERVICE_SID"))
    # sms, or whatsapp (Twilio API for WhatsApp; same webhooks, addresses prefixed with 'whatsapp:').
    twilio_channel: str = field(default_factory=lambda: _choice_env("TWILIO_CHANNEL", "sms", {"sms", "whatsapp"}))
    # WhatsApp sender in E.164 (the Sandbox is +14155238886). Replies go back on the channel the user wrote in.
    twilio_whatsapp_number: str = field(default_factory=lambda: _str_env("TWILIO_WHATSAPP_NUMBER"))
    # Optional Content template (HX...) whose whole body is {{1}}: WhatsApp replies are sent through it, for senders
    # that refuse free-form Body (error 21654). Create it with `python -m genesis sms-whatsapp-template`.
    twilio_whatsapp_content_sid: str = field(default_factory=lambda: _str_env("TWILIO_WHATSAPP_CONTENT_SID"))
    twilio_region: str = field(default_factory=lambda: _str_env("TWILIO_REGION"))
    twilio_edge: str = field(default_factory=lambda: _str_env("TWILIO_EDGE"))

    # ---- Meta WhatsApp Cloud API (Graph API) ----
    whatsapp_provider: str = field(default_factory=lambda: _choice_env("GENESIS_WHATSAPP_PROVIDER", "twilio", {"meta", "twilio", "off"}))
    meta_access_token: str = field(default_factory=lambda: _str_env("META_ACCESS_TOKEN"), repr=False)
    meta_phone_number_id: str = field(default_factory=lambda: _str_env("META_PHONE_NUMBER_ID"))
    meta_business_account_id: str = field(default_factory=lambda: _str_env("META_BUSINESS_ACCOUNT_ID"))
    meta_verify_token: str = field(default_factory=lambda: _str_env("META_VERIFY_TOKEN"), repr=False)
    meta_app_secret: str = field(default_factory=lambda: _str_env("META_APP_SECRET"), repr=False)
    meta_graph_api_version: str = field(default_factory=lambda: _str_env("META_GRAPH_API_VERSION", "v21.0") or "v21.0")
    # Externally visible https origin that Twilio calls (used for signature validation and download links).
    public_base_url: str = field(default_factory=lambda: _str_env("PUBLIC_BASE_URL").rstrip("/"))
    # closed: only GENESIS_SMS_OWNER_NUMBERS; link: others must text LINK <one-time code>; open: anyone gets an isolated guest account.
    # account: numbers must belong to an askweave.xyz account (sign up on the site, then text LINK <code>).
    sms_signup: str = field(default_factory=lambda: _choice_env("GENESIS_SMS_SIGNUP", "link", {"closed", "link", "open", "account"}))
    # Referrals: users text INVITE for a code; friends text JOIN <code> to get a guest account (works in link mode too).
    sms_referrals: bool = field(default_factory=lambda: _choice_env("GENESIS_SMS_REFERRALS", "on", {"on", "off"}) == "on")
    sms_referral_bonus: int = field(default_factory=lambda: _int_env("GENESIS_SMS_REFERRAL_BONUS", 2, 0, 50))
    sms_referral_max: int = field(default_factory=lambda: _int_env("GENESIS_SMS_REFERRAL_MAX", 20, 0, 1000))
    # Accounts (askweave.xyz sign-up). Google sign-in shows only when both are set.
    google_client_id: str = field(default_factory=lambda: _str_env("GOOGLE_CLIENT_ID"))
    google_client_secret: str = field(default_factory=lambda: _str_env("GOOGLE_CLIENT_SECRET"), repr=False)
    user_storage_mb: int = field(default_factory=lambda: _int_env("GENESIS_USER_STORAGE_MB", 50, 1, 10_000))
    max_projects: int = field(default_factory=lambda: _int_env("GENESIS_MAX_PROJECTS", 25, 1, 1000))
    trash_days: int = field(default_factory=lambda: _int_env("GENESIS_TRASH_DAYS", 30, 1, 365))
    sms_owner_numbers: list[str] = field(default_factory=lambda: _list_env("GENESIS_SMS_OWNER_NUMBERS"))
    sms_default_country_code: str = field(default_factory=lambda: _str_env("GENESIS_SMS_DEFAULT_COUNTRY_CODE", "+1"))
    sms_worker: str = field(default_factory=lambda: _choice_env("GENESIS_SMS_WORKER", "embedded", {"embedded", "external"}))
    sms_daily_builds_member: int = field(default_factory=lambda: _int_env("GENESIS_SMS_DAILY_BUILDS", 20, 1, 1000))
    sms_daily_builds_guest: int = field(default_factory=lambda: _int_env("GENESIS_SMS_GUEST_DAILY_BUILDS", 3, 0, 100))
    sms_inbound_per_hour: int = field(default_factory=lambda: _int_env("GENESIS_SMS_INBOUND_PER_HOUR", 30, 5, 1000))
    sms_max_queued: int = field(default_factory=lambda: _int_env("GENESIS_SMS_MAX_QUEUED", 20, 1, 500))
    sms_job_max_attempts: int = field(default_factory=lambda: _int_env("GENESIS_SMS_JOB_MAX_ATTEMPTS", 3, 1, 10))
    sms_progress: str = field(default_factory=lambda: _choice_env("GENESIS_SMS_PROGRESS", "major", {"all", "major", "off"}))
    sms_progress_min_interval: int = field(default_factory=lambda: _int_env("GENESIS_SMS_PROGRESS_MIN_INTERVAL_SECONDS", 60, 0, 3600))
    sms_link_ttl_hours: int = field(default_factory=lambda: _int_env("GENESIS_SMS_LINK_TTL_HOURS", 72, 1, 720))
    # Chat brain: plain-language conversation through the AI backend (Antigravity or the API). off = rules only.
    chat_brain: bool = field(default_factory=lambda: _choice_env("GENESIS_CHAT_BRAIN", "on", {"on", "off"}) == "on")
    chat_timeout: int = field(default_factory=lambda: _int_env("GENESIS_CHAT_TIMEOUT_SECONDS", 25, 5, 600))
    # Model for chat replies only (Gemini API backend). A light model answers in ~2 s; empty = GEMINI_MODEL.
    chat_model: str = field(default_factory=lambda: _str_env("GENESIS_CHAT_MODEL", "gemini-3.5-flash-lite"))
    # A chat reply that hasn't arrived after chat_hedge_seconds is also asked of this model; the first answer wins.
    # Replies took 2-14 s on the lite model alone (and longer when it was busy).
    chat_fallback_model: str = field(default_factory=lambda: _str_env("GENESIS_CHAT_FALLBACK_MODEL", "gemini-2.5-flash"))
    chat_hedge_seconds: float = field(default_factory=lambda: float(_int_env("GENESIS_CHAT_HEDGE_SECONDS", 4, 1, 60)))
    # Chat replies for different people are worked out at the same time (each person's messages stay in order).
    chat_workers: int = field(default_factory=lambda: _int_env("GENESIS_CHAT_WORKERS", 16, 1, 64))
    # Natural Language Generation (NLG): rephrases factual results conversationally. off = deterministic only.
    nlg_enabled: bool = field(default_factory=lambda: _choice_env("GENESIS_NLG", "on", {"on", "off"}) == "on")
    nlg_timeout: int = field(default_factory=lambda: _int_env("GENESIS_NLG_TIMEOUT_SECONDS", 12, 2, 60))
    nlg_model: str = field(default_factory=lambda: _str_env("GENESIS_NLG_MODEL", "gemini-3.5-flash-lite"))
    # Voice notes (WhatsApp/MMS audio) are transcribed locally with Whisper. Models: tiny | base | small | medium.
    voice_enabled: bool = field(default_factory=lambda: _choice_env("GENESIS_VOICE", "on", {"on", "off"}) == "on")
    voice_model: str = field(default_factory=lambda: _choice_env(
        "GENESIS_VOICE_MODEL", "base", {"tiny", "base", "small", "medium", "tiny.en", "base.en", "small.en"}))
    voice_language: str = field(default_factory=lambda: _str_env("GENESIS_VOICE_LANGUAGE", ""))  # "" = auto-detect

    # ---- Website builder: weave's generator (adityapokuri229/weave, src/portfolio/) run through genesis/web/bridge.mjs ----
    # Path to an unmodified weave checkout. Empty = the website builder is unavailable.
    website_engine_dir: str = field(default_factory=lambda: _str_env("GENESIS_WEBSITE_ENGINE_DIR"))
    website_builder: bool = field(default_factory=lambda: _choice_env("GENESIS_WEBSITE_BUILDER", "on", {"on", "off"}) == "on")
    node_bin: str = field(default_factory=lambda: _str_env("GENESIS_NODE_BIN") or shutil.which("node") or "")
    # Design options generated per new website (each is one more large Gemini call).
    website_variants: int = field(default_factory=lambda: _int_env("GENESIS_WEBSITE_VARIANTS", 1, 1, 3))
    # Tried in order when the main model is overloaded (Gemini 503/429) or stalls; each model family has its own
    # capacity. Ordered by measured speed on real builds: 2.5-flash without thinking ~120 tokens/s, while
    # 3.8-flash managed 28-69 tokens/s and once took 6 minutes for one page, so it goes last.
    website_fallback_models: str = field(default_factory=lambda: _str_env("GENESIS_WEBSITE_FALLBACK_MODELS") or
                                         "gemini-2.5-flash,gemini-flash-latest,gemini-3.5-flash-lite,gemini-3.8-flash")
    # Ask the user's style preferences (numbered choices, colours, or "skip") before designing a new website.
    website_ask_style: bool = field(default_factory=lambda: _choice_env("GENESIS_WEBSITE_ASK_STYLE", "on", {"on", "off"}) == "on")
    # Website builds mostly wait on Gemini, so several run at once; Android builds stay CPU-bound
    # (GENESIS_MAX_CONCURRENT_BUILDS).
    # How much Gemini "thinks" before writing a page: low (fast), minimal (fastest), or default (slowest).
    website_thinking: str = field(default_factory=lambda: _choice_env("GENESIS_WEBSITE_THINKING", "low", {"low", "minimal", "default"}))
    website_workers: int = field(default_factory=lambda: _int_env("GENESIS_WEBSITE_WORKERS", 4, 1, 16))
    # Extra pages (about, contact, ...) added to each new website; 0 = one-page sites. Each is one more Gemini call.
    website_pages: int = field(default_factory=lambda: _int_env("GENESIS_WEBSITE_PAGES", 3, 0, 4))
    website_timeout: int = field(default_factory=lambda: _int_env("GENESIS_WEBSITE_TIMEOUT_SECONDS", 1200, 60, 3600))
    # Publishing: off | netlify (owner approves each PUBLISH; needs a token) |
    # weave (every finished website goes live at https://<name>.<GENESIS_SITES_DOMAIN>/, served by this server).
    website_deploy: str = field(default_factory=lambda: _choice_env("GENESIS_WEBSITE_DEPLOY", "off", {"off", "netlify", "weave"}))
    sites_domain: str = field(default_factory=lambda: _str_env("GENESIS_SITES_DOMAIN").lower().strip("."))
    netlify_auth_token: str = field(default_factory=lambda: _str_env("NETLIFY_AUTH_TOKEN"), repr=False)

    @property
    def builds_dir(self) -> Path:
        return self.data_dir / "builds"

    @property
    def sites_dir(self) -> Path:
        return self.data_dir / "sites"

    @property
    def hosted_dir(self) -> Path:
        return self.data_dir / "hosted"

    @property
    def user_sites_dir(self) -> Path:
        """Sites of account holders: <user_sites_dir>/<username>/<project slug>/."""
        return self.data_dir / "hosted-users"

    @property
    def trash_dir(self) -> Path:
        """Deleted projects, restorable until purged after GENESIS_TRASH_DAYS."""
        return self.data_dir / "hosted-trash"

    @property
    def gradle_user_home(self) -> Path:
        return self.data_dir / "gradle-home"

    @property
    def sms_db_path(self) -> Path:
        return self.data_dir / "sms.sqlite3"

    @property
    def whatsapp_sender(self) -> str:
        if self.twilio_whatsapp_number:
            return self.twilio_whatsapp_number
        return self.twilio_phone_number if self.twilio_channel == "whatsapp" else ""

    @property
    def sms_sender(self) -> str:
        return "" if self.twilio_channel == "whatsapp" and not self.twilio_whatsapp_number else self.twilio_phone_number

    @property
    def sms_enabled(self) -> bool:
        """An external messaging channel (Twilio SMS/WhatsApp, Meta WhatsApp, or the log provider) is configured."""
        if self.sms_provider == "log":
            return True
        if self.whatsapp_provider == "meta" and bool(self.meta_access_token and self.meta_phone_number_id):
            return True
        return (self.sms_provider == "twilio" and bool(self.twilio_account_sid and self.twilio_auth_token)
                and bool(self.twilio_phone_number or self.twilio_whatsapp_number or self.twilio_messaging_service_sid))

    def __post_init__(self) -> None:
        if self.ai_provider == "glm":
            # GLM answers first. When it's busy (the free key takes one request at a time), refuses or runs out,
            # the Gemini models take over, if GEMINI_API_KEY is set. GeminiClient and the website bridge send
            # glm-* models to GLM and the rest to Gemini.
            def split(models: str) -> list[str]:
                return [m.strip() for m in models.split(",") if m.strip()]

            glm_backups = split(self.glm_fallback_models)
            gemini_chat = [self.chat_model] if self.gemini_api_key and self.chat_model else []
            gemini_web = []
            if self.gemini_api_key:
                main = self.gemini_model if self.gemini_model != "auto" else "gemini-3.5-flash"
                gemini_web = list(dict.fromkeys([main, *split(self.website_fallback_models)]))
            self.gemini_model = self.chat_model = self.nlg_model = self.glm_model
            self.chat_fallback_model = (glm_backups + gemini_chat + [""])[0]
            self.website_fallback_models = ",".join(glm_backups + gemini_web)

    def sms_issues(self) -> list[str]:
        """Configuration problems that stop the messaging channels from working (names only, never values)."""
        if self.sms_provider == "off" and self.whatsapp_provider == "off":
            return ["GENESIS_SMS_PROVIDER=off", "GENESIS_WHATSAPP_PROVIDER=off"]
        issues = []
        if self.whatsapp_provider == "meta":
            if not self.meta_access_token:
                issues.append("META_ACCESS_TOKEN is missing")
            if not self.meta_phone_number_id:
                issues.append("META_PHONE_NUMBER_ID is missing")
        if self.sms_provider == "twilio":
            if not self.twilio_account_sid.startswith("AC"):
                issues.append("TWILIO_ACCOUNT_SID is missing or does not start with AC")
            if not self.twilio_auth_token:
                issues.append("TWILIO_AUTH_TOKEN is missing")
            if not (self.twilio_phone_number or self.twilio_whatsapp_number or self.twilio_messaging_service_sid):
                issues.append("set TWILIO_WHATSAPP_NUMBER (WhatsApp), TWILIO_PHONE_NUMBER (SMS) or TWILIO_MESSAGING_SERVICE_SID")
        if not self.public_base_url.startswith("https://"):
            issues.append("PUBLIC_BASE_URL must be the public https:// URL that forwards to Genesis")
        return issues

    def build_tools_dir(self) -> Path | None:
        if not self.android_home:
            return None
        bt = self.android_home / "build-tools"
        if not bt.is_dir():
            return None
        versions = sorted((p for p in bt.iterdir() if p.is_dir()), key=lambda p: [int(x) if x.isdigit() else 0 for x in re.split(r"[.-]", p.name)])
        return versions[-1] if versions else None

    def doctor(self) -> dict:
        """Report prerequisites without revealing secrets."""
        checks = []

        def add(name: str, ok: bool, detail: str, fix: str = "") -> None:
            checks.append({"name": name, "ok": ok, "detail": detail, "fix": "" if ok else fix})

        # Python dependencies
        missing_deps = []
        for mod, pkg in (("fastapi", "fastapi"), ("uvicorn", "uvicorn"), ("pydantic", "pydantic"), ("dotenv", "python-dotenv")):
            try:
                __import__(mod)
            except ImportError:
                missing_deps.append(pkg)
        if self.ai_backend == "api":
            try:
                __import__("google.genai")
            except ImportError:
                missing_deps.append("google-genai")
        if self.voice_enabled:
            try:
                __import__("faster_whisper")
            except ImportError:
                missing_deps.append("faster-whisper")
        add("Python dependencies", not missing_deps,
            "all required packages installed" if not missing_deps else f"missing packages: {', '.join(missing_deps)}",
            "Run: pip install -r requirements.txt")

        if self.ai_backend == "antigravity":
            from .core.antigravity import AntigravityClient
            problem = AntigravityClient(self).problem()
            add("Antigravity bridge", not problem, problem or "running inside an Antigravity terminal",
                "Start Genesis from a terminal inside the Antigravity app")
        else:
            add("Gemini API key", bool(self.gemini_api_key),
                "GEMINI_API_KEY is set" if self.gemini_api_key else "GEMINI_API_KEY is missing",
                "Create a key at https://aistudio.google.com/apikey and put GEMINI_API_KEY=... in .env")

        if self.whatsapp_provider == "meta":
            meta_missing = []
            if not self.meta_access_token:
                meta_missing.append("META_ACCESS_TOKEN")
            if not self.meta_phone_number_id:
                meta_missing.append("META_PHONE_NUMBER_ID")
            if not self.meta_verify_token:
                meta_missing.append("META_VERIFY_TOKEN")
            if not self.meta_app_secret:
                meta_missing.append("META_APP_SECRET")
            add("Meta WhatsApp", not meta_missing,
                f"configured (phone number ID: {self.meta_phone_number_id})" if not meta_missing else f"missing: {', '.join(meta_missing)}",
                "Set META_ACCESS_TOKEN, META_PHONE_NUMBER_ID, META_VERIFY_TOKEN, and META_APP_SECRET in .env")
        elif self.whatsapp_provider == "twilio" or self.sms_provider == "twilio":
            tw_issues = [i for i in self.sms_issues() if not i.startswith("PUBLIC_BASE_URL")]
            add("Twilio configuration", not tw_issues,
                "Twilio credentials configured" if not tw_issues else f"issues: {'; '.join(tw_issues)}",
                "Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO phone number in .env")

        pub_ok = bool(self.public_base_url and self.public_base_url.startswith("https://"))
        add("Public URL", pub_ok,
            self.public_base_url if pub_ok else ("PUBLIC_BASE_URL is not set" if not self.public_base_url else "PUBLIC_BASE_URL must begin with https://"),
            "Start via scripts/genesis_public.ps1 or set PUBLIC_BASE_URL=https://... in .env")

        cf = find_cloudflared()
        add("Cloudflare tunnel", cf is not None,
            f"cloudflared available at {cf}" if cf else "cloudflared not found",
            "Install cloudflared (winget install Cloudflare.cloudflared or brew install cloudflared)")

        major = java_major_version(self.java_home) if self.java_home else None
        add("JDK 17+", major is not None and major >= 17,
            f"Java {major} at {self.java_home}" if major else "No JDK 17+ found",
            "Install a JDK 17 (e.g. Eclipse Temurin 17 or Android Studio jbr) and set JAVA_HOME in .env")
        add("Android SDK", self.android_home is not None,
            str(self.android_home) if self.android_home else "Android SDK not found",
            "Install Android cmdline-tools and set ANDROID_HOME in .env")
        platform_ok = bool(self.android_home and (self.android_home / "platforms/android-35").is_dir())
        add("Android platform 35", platform_ok, "platforms;android-35 installed" if platform_ok else "platforms;android-35 missing",
            'Run: sdkmanager "platforms;android-35" "build-tools;35.0.0"')
        licenses_ok = bool(self.android_home and (self.android_home / "licenses/android-sdk-license").is_file())
        add("Android SDK license", licenses_ok, "accepted" if licenses_ok else "not accepted",
            "Run: sdkmanager --licenses")
        wrapper_ok = ((self.template_dir / "gradlew").is_file() or (self.template_dir / "gradlew.bat").is_file()) and (self.template_dir / "gradle/wrapper/gradle-wrapper.jar").is_file()
        add("Gradle wrapper", wrapper_ok, "template wrapper present" if wrapper_ok else "android_template/gradlew or gradlew.bat missing",
            "Restore android_template/gradlew, gradlew.bat and gradle/wrapper from git")

        core_checks = [c for c in checks if c["name"] in ("Gemini API key", "Antigravity bridge", "JDK 17+", "Android SDK", "Android platform 35", "Android SDK license", "Gradle wrapper")]
        return {"ready": all(c["ok"] for c in core_checks), "checks": checks}

    def website_problem(self) -> str:
        """Why the website builder cannot run (empty when it can). Kept out of doctor(), which gates Android builds."""
        if not self.website_builder:
            return "the website builder is switched off (GENESIS_WEBSITE_BUILDER=off)"
        if not self.website_engine_dir:
            return "no website engine is configured (GENESIS_WEBSITE_ENGINE_DIR)"
        engine = Path(self.website_engine_dir).expanduser()
        if not (engine / "src/portfolio/bespokeHtmlGenerator.js").is_file():
            return f"the website engine was not found at {engine} (expected a weave checkout with src/portfolio/)"
        if not self.node_bin or not Path(self.node_bin).is_file():
            return "Node.js was not found (install Node 20+ or set GENESIS_NODE_BIN)"
        if self.ai_backend != "api":
            return "the website engine needs the Gemini API backend (GENESIS_AI_BACKEND=api)"
        if not self.gemini_api_key and not (self.ai_provider == "glm" and self.glm_api_key):
            return "GEMINI_API_KEY is missing"
        return ""

    def website_doctor(self) -> list[dict]:
        problem = self.website_problem()
        checks = [{"name": "Website builder", "ok": not problem,
                   "detail": problem or f"weave engine at {Path(self.website_engine_dir).expanduser()}",
                   "fix": "" if not problem else "See README: Website builder"}]
        if self.website_deploy == "weave":
            checks.append({"name": "Website publishing", "ok": bool(self.sites_domain),
                           "detail": f"Weave hosting: every website goes live at https://<name>.{self.sites_domain}/"
                                     if self.sites_domain else "GENESIS_SITES_DOMAIN is not set",
                           "fix": "" if self.sites_domain else "Set GENESIS_SITES_DOMAIN (e.g. weave.xyz) and point *.domain at this server"})
            return checks
        publish = self.website_deploy == "netlify" and bool(self.netlify_auth_token)
        checks.append({"name": "Website publishing", "ok": True,
                       "detail": "Netlify (owner approval per publish)" if publish else
                                 "off: websites get private previews only (set GENESIS_WEBSITE_DEPLOY=netlify and NETLIFY_AUTH_TOKEN)",
                       "fix": ""})
        return checks


settings = Settings()

