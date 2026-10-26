"""Rules for usernames (permanent; they become <username>.askweave.xyz) and call signs (what the bot calls you).

A username is a hostname label, so it is lowercase letters, digits and single hyphens. It must not be a name the
platform needs, an offensive word, or a well-known brand (also spelled with lookalike characters, e.g. "paypa1"),
because a subdomain like paypal.askweave.xyz invites phishing."""

from __future__ import annotations

import re

from ..web.hosting import RESERVED as SITE_RESERVED

USERNAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,18}[a-z0-9]$")
CALL_SIGN_RE = re.compile(r"^[\w .'\-]{1,30}$", re.UNICODE)

RESERVED = SITE_RESERVED | {
    "root", "about", "terms", "privacy", "report", "abuse", "account", "accounts", "signup", "signin", "start", "billing",
    "pay", "payment", "payments", "security", "official", "team", "staff", "askweave", "ns1", "ns2", "imap", "pop",
    "email", "news", "info", "contact", "owner", "system", "null", "undefined", "home", "index", "user", "users",
}
# Impersonation: matched anywhere in the (de-lookaliked) name.
BRANDS = (
    "google", "gmail", "youtube", "facebook", "instagram", "whatsapp", "apple", "icloud", "microsoft", "outlook",
    "amazon", "paypal", "netflix", "twitter", "tiktok", "snapchat", "linkedin", "telegram", "spotify", "adobe",
    "github", "openai", "chatgpt", "anthropic", "gemini", "binance", "coinbase", "metamask", "paytm", "phonepe",
    "googlepay", "flipkart", "myntra", "swiggy", "zomato", "airtel", "aadhaar", "uidai", "irctc", "hdfc", "icici",
    "kotak", "axisbank", "yesbank", "bankof", "visa", "mastercard", "rupay", "verify", "signin", "login", "wallet",
)
# Short brands only as a whole hyphen-separated part ("sbi" blocks "sbi-help", not "sbiswas").
SHORT_BRANDS = {"sbi", "rbi", "aws", "upi", "gov", "govt", "meta", "jio", "uber", "pnb", "npci", "bank", "x"}
OFFENSIVE = (
    "fuck", "shit", "bitch", "cunt", "nigg", "fag", "porn", "xxx", "rape", "nazi", "hitler", "slut", "whore", "dick",
    "pussy", "chutiya", "madarchod", "bhenchod", "behenchod", "bsdk", "gandu", "randi", "lund", "chod", "harami",
)
LOOKALIKE = str.maketrans({"0": "o", "1": "l", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b", "9": "g", "-": ""})


def _variants(name: str) -> set[str]:
    """The name with common lookalike tricks undone: digits for letters, hyphens, rn for m, vv for w, l for i."""
    base = name.translate(LOOKALIKE)
    out = {base, base.replace("rn", "m").replace("vv", "w")}
    return out | {v.replace("l", "i") for v in out}


def username_problem(name: str) -> str:
    """Why a username can't be used, or "" if it can."""
    if not USERNAME_RE.fullmatch(name or ""):
        return "Use 3-20 lowercase letters, digits or hyphens, starting with a letter and not ending with a hyphen."
    if "--" in name:
        return "Use single hyphens only."
    if name in RESERVED:
        return "That name is reserved."
    variants = _variants(name)
    if any(w in v for v in variants for w in OFFENSIVE):
        return "Please choose a different name."
    if any(b in v for v in variants for b in BRANDS) or any(part in SHORT_BRANDS for part in name.split("-")):
        return "That name looks like a company or brand, so it can't be used as a Weave address."
    return ""


def clean_call_sign(raw: str) -> str:
    """The call sign to store, or "" if it isn't allowed."""
    text = " ".join((raw or "").split())
    if not CALL_SIGN_RE.fullmatch(text) or "http" in text.lower() or "www." in text.lower():
        return ""
    variants = _variants(text.lower().replace(" ", ""))
    return "" if any(w in v for v in variants for w in OFFENSIVE) else text
