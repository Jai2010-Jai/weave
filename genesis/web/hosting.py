"""Weave hosting: every finished website is served from this server at https://<name>.<GENESIS_SITES_DOMAIN>/.

Publishing copies one validated website version into data/hosted/<name>/ (atomic swap, so visitors never see a
half-written site). The server's Host-header router (genesis.server) serves those files to anyone, inside the
same CSP sandbox as private previews. Needs a wildcard DNS record (*.domain) pointing at this server or tunnel."""

from __future__ import annotations

import re
import secrets
import shutil
import time
from pathlib import Path

from .validate import PAGE_NAME_RE

# Labels that must never become a customer site (they name the platform itself or look official).
RESERVED = {"www", "api", "app", "admin", "mail", "smtp", "ftp", "chat", "status", "docs", "blog", "help",
            "support", "dashboard", "login", "auth", "weave", "genesis", "static", "cdn", "assets", "dev",
            "staging", "test", "preview", "download", "webhooks"}
LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])?$")


def slugify(brand: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (brand or "").lower()).strip("-")[:30].strip("-")
    return slug if len(slug) >= 3 else "site"


def site_label(hosted_dir: Path, brand: str, current: str | None = None) -> str:
    """The subdomain for a project: its existing one, else a free one derived from the brand name."""
    if current and LABEL_RE.fullmatch(current):
        return current
    base = slugify(brand)
    if base not in RESERVED and not (hosted_dir / base).exists():
        return base
    for _ in range(20):
        label = f"{base}-{secrets.token_hex(2)}"
        if not (hosted_dir / label).exists():
            return label
    raise RuntimeError("could not find a free site name")


def site_url(label: str, domain: str) -> str:
    return f"https://{label}.{domain}/"


def publish_hosted(hosted_dir: Path, site_dir: Path, label: str) -> None:
    """Replace data/hosted/<label>/ with the files of one validated website version."""
    if not LABEL_RE.fullmatch(label) or label in RESERVED:
        raise ValueError(f"invalid site name: {label}")
    _publish_dir(hosted_dir, site_dir, label)


def _publish_dir(hosted_dir: Path, site_dir: Path, label: str) -> None:
    if not (site_dir / "index.html").is_file():
        raise ValueError("there is no website to publish")
    hosted_dir.mkdir(parents=True, exist_ok=True)
    tmp = hosted_dir / f".{label}.{secrets.token_hex(4)}"
    tmp.mkdir()
    for p in site_dir.iterdir():
        if p.is_file() and not p.is_symlink() and PAGE_NAME_RE.fullmatch(p.name):
            shutil.copyfile(p, tmp / p.name)
    final, old = hosted_dir / label, hosted_dir / f".{label}.old.{secrets.token_hex(4)}"
    if final.exists():
        final.rename(old)
    tmp.rename(final)
    shutil.rmtree(old, ignore_errors=True)


def label_for_host(host: str, domain: str) -> str | None:
    """'bakery.weave.xyz' -> 'bakery' when domain is 'weave.xyz'; None for anything else."""
    host, domain = (host or "").lower().rstrip("."), (domain or "").lower().strip(".")
    if not domain or not host.endswith("." + domain):
        return None
    label = host[: -len(domain) - 1]
    if "." in label or not LABEL_RE.fullmatch(label) or label in RESERVED:
        return None
    return label


def hosted_file(hosted_dir: Path, label: str, path: str) -> Path | None:
    """The file to serve for a request path on a hosted site, or None (404)."""
    name = path.lstrip("/") or "index.html"
    if name.endswith("/"):
        name += "index.html"
    if not PAGE_NAME_RE.fullmatch(name):
        return None
    root = (hosted_dir / label).resolve()
    f = (root / name).resolve()
    return f if f.parent == root and f.is_file() else None


# ---------- account sites: https://<username>.<domain>/<slug>/ ----------
def project_slug(name: str, taken) -> str:
    """A free path for a project under its owner's subdomain; `taken(slug)` says whether it's in use."""
    base = slugify(name)
    if not taken(base):
        return base
    for _ in range(20):
        slug = f"{base}-{secrets.token_hex(2)}"
        if not taken(slug):
            return slug
    raise RuntimeError("could not find a free project path")


def project_url(username: str, slug: str, domain: str) -> str:
    return f"https://{username}.{domain}/{slug}/"


def publish_project(user_root: Path, site_dir: Path, slug: str) -> None:
    """Replace <user_root>/<slug>/ with one validated website version (atomic, like publish_hosted)."""
    if not LABEL_RE.fullmatch(slug):
        raise ValueError(f"invalid project path: {slug}")
    _publish_dir(user_root, site_dir, slug)


def user_file(user_root: Path, path: str) -> tuple[str, Path | str | None]:
    """What to serve for a path on <username>.<domain>: ("index", None) for the user's home page,
    ("redirect", "/slug/") to add the trailing slash relative links need, ("file", path) or ("missing", None)."""
    parts = path.strip("/").split("/") if path.strip("/") else []
    if not parts:
        return "index", None
    slug = parts[0]
    if not LABEL_RE.fullmatch(slug) or len(parts) > 2:
        return "missing", None
    if len(parts) == 1 and not path.endswith("/"):
        return ("redirect", f"/{slug}/") if (user_root / slug).is_dir() else ("missing", None)
    page = parts[1] if len(parts) == 2 and parts[1] else "index.html"
    if not PAGE_NAME_RE.fullmatch(page):
        return "missing", None
    root = (user_root / slug).resolve()
    f = (root / page).resolve()
    return ("file", f) if f.parent == root and f.is_file() else ("missing", None)


def user_index_html(username: str, projects: list[tuple[str, str]], report_url: str = "") -> str:
    """The page at https://<username>.<domain>/: links to the user's live sites."""
    esc = lambda t: t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")  # noqa: E731
    items = "".join(f'<li><a href="/{esc(slug)}/">{esc(name)}</a></li>' for name, slug in projects)
    body = f"<ul>{items}</ul>" if items else "<p>No sites here yet.</p>"
    return ("<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" "
            "content=\"width=device-width,initial-scale=1\">"
            f"<title>{esc(username)} on Weave</title><style>body{{font:16px/1.6 system-ui,sans-serif;max-width:40rem;"
            "margin:3rem auto;padding:0 1rem;color:#1a1a1a}a{color:#4f46e5}li{margin:.4rem 0}"
            "footer{margin-top:3rem;font-size:13px;color:#777}</style></head>"
            f"<body><h1>{esc(username)}</h1>{body}<footer>Made with Weave"
            + (f' &middot; <a href="{esc(report_url)}">Report abuse</a>' if report_url else "")
            + "</footer></body></html>")


def move_to_trash(user_root: Path, trash_root: Path, username: str, slug: str) -> str | None:
    """Take a project's live files offline into the trash. Returns the trash folder name (None if not hosted)."""
    src = user_root / slug
    if not src.is_dir():
        return None
    dest_dir = trash_root / username
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = f"{slug}.{int(time.time())}"
    src.rename(dest_dir / name)
    return name


def restore_from_trash(user_root: Path, trash_root: Path, username: str, trash_name: str, slug: str) -> bool:
    src = trash_root / username / trash_name
    dest = user_root / slug
    if not LABEL_RE.fullmatch(slug) or not src.is_dir() or dest.exists():
        return False
    user_root.mkdir(parents=True, exist_ok=True)
    src.rename(dest)
    return True


def purge_trash(trash_root: Path, days: int) -> int:
    """Delete trashed projects older than `days`. Returns how many were removed."""
    if not trash_root.is_dir():
        return 0
    cutoff, n = time.time() - days * 86_400, 0
    for d in trash_root.glob("*/*"):
        stamp = d.name.rsplit(".", 1)[-1]  # trashed at (move_to_trash); a rename keeps the old mtime
        if d.is_dir() and (int(stamp) if stamp.isdigit() else d.stat().st_mtime) < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n


def dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.is_dir() else 0
