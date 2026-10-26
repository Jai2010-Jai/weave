"""Checks for generated website files. The HTML is untrusted: it is never executed on the host, only served
to browsers inside a CSP sandbox, but obviously unsafe or broken output is rejected before anyone sees it."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

MAX_PAGE_BYTES = 1_000_000
MAX_PAGES = 8  # index, up to 2 design options, up to 4 extra pages
# index.html, design options (option2/3.html) and extra pages such as about.html or contact-us.html.
PAGE_NAME_RE = re.compile(r"^(?:index|option[23]|[a-z][a-z0-9-]{1,19})\.html$")
# Script origins the preview CSP allows. weave's generator uses the Tailwind CDN; the others are common CDNs.
SCRIPT_HOSTS = ("cdn.tailwindcss.com", "cdn.jsdelivr.net", "unpkg.com", "cdnjs.cloudflare.com")


class WebsiteValidationError(ValueError):
    pass


class _Scan(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: set[str] = set()
        self.problems: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        self.tags.add(tag)
        if tag == "script" and a.get("src"):
            src = a["src"].strip()
            parts = urlsplit(src if "//" in src else "https://invalid.local/" + src)
            if parts.scheme != "https" or parts.hostname not in SCRIPT_HOSTS:
                self.problems.append(f"script from a host that is not allowed: {src[:120]}")
        if tag in ("iframe", "embed", "object", "base"):
            self.problems.append(f"<{tag}> is not allowed")
        if tag == "meta" and a.get("http-equiv", "").lower() == "refresh":
            self.problems.append("meta refresh redirects are not allowed")
        if tag == "form" and a.get("action", "").strip() not in ("", "#"):
            self.problems.append("forms must not submit to other sites")


def validate_page(html: str) -> None:
    size = len(html.encode("utf-8"))
    if size > MAX_PAGE_BYTES:
        raise WebsiteValidationError(f"page is too large ({size:,} bytes)")
    low = html.lower()
    if "<html" not in low or "</html>" not in low or "<body" not in low:
        raise WebsiteValidationError("page is not a complete HTML document")
    scan = _Scan()
    scan.feed(html)
    scan.close()
    if scan.problems:
        raise WebsiteValidationError("; ".join(sorted(set(scan.problems))[:5]))


SENSITIVE_WORDS = {"password", "passwd", "passcode", "pwd", "pass", "card", "cardnumber", "cc", "ccnumber", "cvv", "cvc",
                   "otp", "ssn", "aadhaar", "aadhar", "pan", "iban", "routing", "seed", "mnemonic", "cc-number",
                   "cc-csc", "cc-exp", "one-time-code", "current-password", "new-password"}


class _Fields(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "input":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        if a.get("type", "").lower() == "password":
            self.found.append("password field")
        elif a.get("autocomplete", "").lower() in SENSITIVE_WORDS or any(
                w in SENSITIVE_WORDS for k in ("name", "id")
                for w in re.split(r"[^a-z0-9]+", re.sub(r"([a-z])([A-Z])", r"\1_\2", a.get(k, "")).lower())):
            self.found.append(f"{a.get('name') or a.get('id') or a.get('autocomplete')} field")


def public_problem(site_dir: Path) -> str:
    """Why a site can't go on a public address, or "". Pages that ask for passwords, card numbers or one-time codes
    are the shape of phishing, so they stay on the private preview link only."""
    for p in sorted(site_dir.glob("*.html")):
        scan = _Fields()
        scan.feed(p.read_text(encoding="utf-8", errors="replace"))
        if scan.found:
            return f"{p.name} asks for sensitive details ({scan.found[0]})"
    return ""


def validate_site(site_dir: Path) -> list[dict]:
    """Validate every file the bridge wrote. Returns [{file, bytes}]. Raises WebsiteValidationError."""
    files = sorted(p for p in site_dir.iterdir()) if site_dir.is_dir() else []
    if not any(p.name == "index.html" for p in files):
        raise WebsiteValidationError("index.html was not produced")
    if len(files) > MAX_PAGES:
        raise WebsiteValidationError("too many files produced")
    out = []
    for p in files:
        if not p.is_file() or p.is_symlink() or not PAGE_NAME_RE.fullmatch(p.name):
            raise WebsiteValidationError(f"unexpected output file: {p.name}")
        validate_page(p.read_text(encoding="utf-8"))
        out.append({"file": p.name, "bytes": p.stat().st_size})
    return out
