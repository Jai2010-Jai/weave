"""Weave hosting: every finished website is served from this server at https://<name>.<GENESIS_SITES_DOMAIN>/.

Publishing copies one validated website version into data/hosted/<name>/ (atomic swap, so visitors never see a
half-written site). The server's Host-header router (genesis.server) serves those files to anyone, inside the
same CSP sandbox as private previews. Needs a wildcard DNS record (*.domain) pointing at this server or tunnel."""

from __future__ import annotations

import re
import secrets
import shutil
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
