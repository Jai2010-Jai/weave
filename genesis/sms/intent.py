"""Plain-English understanding for chat messages ("send me the link again", "make the buttons bigger").

Rule-based and instant: runs inside the webhook, so it never calls a model. Exact commands (STATUS, USE 2,
...) still work; this decides what everything else means. When a message could be either a new app or a
change, the service asks instead of guessing, because a wrong guess starts a paid build.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

NEW_APP = re.compile(
    r"\b(?:build|create|make|generate|design|develop|start|code|write)\s+(?:me\s+|us\s+|for\s+me\s+)?"
    r"(?:a|an|another|one|my)\s+(?:new\s+)?(?:[\w'-]+\s+){0,6}?(?:app|application|tracker|calculator|planner|"
    r"journal|timer|counter|game|manager|organi[sz]er|flashcards?|quiz|to-?do list|notes?|diary|log|budget)\b"
    r"|\b(?:new|another|second|separate|different|fresh)\s+app\b"
    r"|\b(?:i\s+(?:want|need|would like|'d like)|can (?:you|u) (?:make|build|create)|could (?:you|u) (?:make|build|create))\s+"
    r"(?:a|an|another|one|me a|me an)\s+(?:[\w'-]+\s+){0,6}?(?:app|application|game|tracker|calculator)\b"
    r"|^(?:an?\s+)?(?:[\w'-]+\s+){0,4}?app\s+(?:that|which|to|for|where|with)\b", re.I)

_SITE = r"(?:web\s*sites?|web\s*pages?|landing\s+pages?|home\s*pages?|portfolio(?:\s+site)?|site)"
NEW_SITE = re.compile(
    r"\b(?:build|create|make|generate|design|develop|start|code|write|launch|set\s+up)\s+(?:me\s+|us\s+|for\s+me\s+)?"
    rf"(?:a|an|another|one)\s+(?:new\s+)?(?:[\w'-]+\s+){{0,6}}?{_SITE}\b"
    r"|\b(?:i\s+(?:want|need|would like|'d like)|can you (?:make|build|create|design)|could you (?:make|build|create|design))\s+"
    rf"(?:a|an|another|one|me a|me an)\s+(?:[\w'-]+\s+){{0,6}}?{_SITE}\b"
    r"|\b(?:new|another|second|separate|different)\s+(?:web\s*site|site|landing\s+page)\b", re.I)
# What kind of software a request is for. "app" on its own means an Android app (what Genesis always built).
WEBSITE_WORDS = re.compile(r"\b(?:web\s*sites?|web\s*pages?|landing\s+pages?|home\s*pages?|portfolio\s+site|"
                           r"site\s+for|online\s+presence|domain|seo)\b", re.I)
ANDROID_WORDS = re.compile(r"\b(?:android|apk|play\s+store|(?:mobile|phone)\s+app|on\s+my\s+phone|install)\b", re.I)
APP_NOUNS = re.compile(r"\b(?:apps?|application|tracker|calculator|planner|journal|timer|counter|game|organi[sz]er|"
                       r"flashcards?|quiz|to-?do\s+list|notes?\s+app|diary|budget(?:ing)?\s+app)\b", re.I)
WEB_APP = re.compile(r"\bweb\s*-?\s*apps?\b", re.I)


def target_kind(text: str) -> str | None:
    """'website', 'android', or None when the request doesn't say (then Genesis asks)."""
    if WEB_APP.search(text):
        return None  # could be either: a site, or an app; ask
    web, android = bool(WEBSITE_WORDS.search(text) or re.search(rf"\b{_SITE}\b", text, re.I)), bool(ANDROID_WORDS.search(text))
    if web and not android:
        return "website"
    if android and not web:
        return "android"
    if web and android:
        return None
    return "android" if APP_NOUNS.search(text) else None


_CREATE = r"\b(?:make|build|create|generate|want|need|turn|convert|give\s+me|get\s+me|send\s+me)\b"
WANTS_ANDROID = re.compile(_CREATE + r".{0,80}?\b(?:apk|android|(?:phone|mobile)\s+app|app\s+version|(?:an?|the)\s+app)\b"
                           r"|\b(?:apk|android|app|phone|mobile)\s+version\b", re.I)
WANTS_WEBSITE = re.compile(_CREATE + r".{0,80}?\b(?:web\s*site|landing\s+page|web\s+page|homepage)\b"
                           r"|\b(?:web\s*site|web)\s+version\b", re.I)


def other_kind_requested(text: str, current: str) -> str | None:
    """'android' when someone working on a website asks for an app/APK of it (or 'website' the other way round):
    that is a new project of the other kind, never a change to the current one."""
    if current == "website" and WANTS_ANDROID.search(text):
        return "android"
    if current == "android" and WANTS_WEBSITE.search(text):
        return "website"
    return None


def answer_kind(text: str) -> str | None:
    """Reply to "Android app or website?": 'android', 'website', 'no' or None."""
    t = text.strip().lower()
    if len(t.split()) > 8:
        return None
    if re.search(r"\b(?:web\s*site|site|web|page|landing|2)\b", t):
        return "website"
    if re.search(r"\b(?:app|android|apk|phone|mobile|1)\b", t):
        return "android"
    if NO.search(t):
        return "no"
    return None


PUBLISH_REQ = re.compile(
    r"^(?:please\s+|can\s+you\s+|could\s+you\s+)?(?:publish|deploy|go\s+live|put|make|launch|ship)\s*"
    r"(?:it|this|that|the\s+(?:site|website|page)|my\s+(?:site|website|page))?\s*"
    r"(?:live|online|on\s+the\s+(?:internet|web)|public(?:ly)?|to\s+netlify)?(?:\s+please)?[\s.!?]*$", re.I)
STATUS_WHAT = re.compile(r"\bwhat\s+are\s+(?:you|u)\s+(?:building|working\s+on|making|doing)\b"
                         r"|\bwhat(?:'s| is)\s+(?:being\s+)?(?:built|building)\b", re.I)

CHANGE = re.compile(
    r"\b(?:add|remove|delete|change|make\s+(?:it|the|them|this|that|everything|all|each|my)|put|rename|fix|"
    r"replace|increase|decrease|bigger|smaller|larger|darker|lighter|brighter|move|swap|switch\s+(?:the|it)|"
    r"turn\s+(?:the|it|on|off)|use\s+(?:a|an|the)|include|show|hide|let\s+me|allow|should|update|edit|tweak|"
    r"improve|redesign|instead|also|color|colour|theme|font|button|screen|page|icon|layout|dark\s*mode|"
    r"light\s*mode)\b", re.I)


def _rx(*patterns: str) -> re.Pattern:
    return re.compile("|".join(patterns), re.I)


SMALL = r"[\s!.?,:)(*~😊🙂👍🙏❤️🔥💯]*"
_HELLO = (r"(?:hi+|hey+|hello+|hiya|yo+|sup|wa+s+up|whats\s*up|what'?s\s+up|wazzup|hola|namaste|good (?:morning|afternoon|evening)|"
          r"there|genesis|weave|bro|bruh|man|buddy|dude|how\s+(?:are|r)\s+(?:you|u)|hru)")
GREETING = _rx(rf"^{_HELLO}(?:[\s,!.?]+{_HELLO})*{SMALL}$")
_ACK = (r"(?:thanks?(?: you)?|thank u|thx|ty|tysm|cheers|ok(?:ay)?|kk?|cool|nice|great|awesome|perfect|amazing|"
        r"sweet|got it|alright|sure|lol|haha+|wow|love it|works|it works|so much|a lot|bro|man|buddy|dude|genesis|weave|"
        r"👍|🙏|❤️|🔥)")
THANKS = _rx(rf"^{_ACK}(?:[\s,!.]+{_ACK})*{SMALL}$")
HELP = _rx(r"^(?:help|menu|commands|options)\b", r"\bwhat can (?:you|u) do\b", r"\bhow (?:does|do) (?:this|it|you) work",
           r"\bhow (?:do|can) i use\b", r"^(?:what|who) are (?:you|u)\b")
STATUS = _rx(r"\b(?:status|progress|updates?|eta)\b\??$", r"\bhow(?:'?s| is)\s+(?:it|my app|my site|my website|the build|that|everything)\s+going\b",
             r"\b(?:is|are)\s+(?:it|my app|the app|the build|my (?:web)?site|the (?:web)?site)\s+(?:ready|done|finished|built)\b",
             r"\bhow (?:long|much longer)\b", r"\bwhat(?:'?s| is) (?:happening|going on)\b", r"\bany (?:updates?|news)\b",
             r"\bwhere(?:'s| is) my app\b", r"\bdone yet\b", r"\bready yet\b",
             r"^(?:are\s+|r\s+)?(?:u|you|it)\s+(?:done|finished|ready)\b",
             r"^(?:respond|reply|answer)(?:\s+(?:me|pls|please|bro|brh|bruh|man))*[\s!?.]*$")
# "don't make a new site, just update that": a change, even though it says "make a new site".
NOT_NEW = _rx(r"\b(?:don'?t|dont|do not|not|no|without)\s+(?:make|making|create|creating|build|building|start)?\s*"
              r"(?:a\s+|another\s+)?(?:new|another|separate)\b")
SITE_WORDS = {"website", "site", "web", "page", "webpage", "landing", "online", "for", "my", "our", "a", "an", "the",
              "it", "this", "that", "is", "ready", "done", "yet", "of", "your", "ur"}
PROJECTS = _rx(r"\b(?:list|show|see|what are)\s+(?:me\s+)?(?:all\s+)?(?:my\s+)?(?:apps|projects)\b",
               r"^(?:my\s+)?(?:apps|projects)\??$", r"\bwhat apps\b", r"\bwhich apps\b")
DOWNLOAD = _rx(r"\b(?:send|give|resend|share|get|need|want)\s+(?:me\s+)?(?:the\s+|a\s+|my\s+)?(?:new\s+)?"
               r"(?:download\s+)?(?:link|apk|download|file)\b",
               r"\bwhere(?:'s| is| do i get)\s+(?:the\s+)?(?:link|apk|download)\b", r"^(?:the\s+)?(?:link|apk|download)"
               r"(?:\s+(?:please|pls|again))?\??$", r"\bdownload\s+(?:it|link|again|the app)\b", r"\bhow do i (?:download|install)\b")
CANCEL = _rx(r"^(?:please\s+)?(?:cancel|abort|stop|kill|halt)\s+(?:it|this|that|(?:the\s+|my\s+|this\s+)?build|building|the\s+app)\b",
             r"^(?:abort|never\s*mind|nvm|forget it)\b")
RETRY = _rx(r"^(?:retry|try again|again|redo|one more time|try it again|rebuild|build it again)\b")
SWITCH = re.compile(r"^(?:switch\s+to|use|open|go\s+(?:back\s+)?to|work\s+on|select|pick|back\s+to)\s+"
                    r"(?:the\s+|my\s+)?(?:app\s+|project\s+)?(?:#|no\.?\s*|number\s+)?(?P<target>.+?)(?:\s+(?:app|one|project))?"
                    r"[\s.!?]*$", re.I)
YES = _rx(r"^(?:y|yes|yeah|yep|yup|sure|ok(?:ay)?|do it|go|go ahead|change it|change)\b")
NEW_WORD = _rx(r"^(?:new|new app|a new (?:one|app)|separate|separate app|make (?:a )?new (?:one|app)|start (?:a )?new)\b")
QUESTION = _rx(r"\?\s*$", r"^(?:can|could|do|does|are|is|will|would|what|how|why|who|where|when|which|should)\b")
REQUEST = _rx(r"^(?:can|could|would|will)\s+(?:you|u)\b")  # "can you add X" is a request, not a question
FILLER = {"i", "want", "a", "an", "new", "app", "application", "make", "me", "build", "create", "please", "pls", "can",
          "you", "u", "another", "one", "the", "to", "need", "would", "like", "id", "i'd", "just", "some", "for", "us",
          "lets", "let's", "do", "start", "now", "ok", "okay", "yes", "different", "separate", "fresh"}
NO = _rx(r"^(?:no|nope|nah|cancel|never\s*mind|nvm)\b")


@dataclass
class Intent:
    kind: str  # greeting|thanks|help|status|projects|download|cancel|retry|switch|publish|new|change|ambiguous|unclear
    arg: str = ""


def classify(text: str, *, has_projects: bool, has_active: bool, project_names: dict[int, str] | None = None) -> Intent:
    t = re.sub(r"\s+", " ", text.strip())
    low = t.lower()
    if not t:
        return Intent("unclear")
    if STATUS_WHAT.search(t):
        return Intent("status")
    if PUBLISH_REQ.match(t) and re.search(r"\b(?:publish|deploy|live|online|public|netlify)", t, re.I):
        return Intent("publish")
    for kind, rx in (("greeting", GREETING), ("thanks", THANKS), ("help", HELP), ("cancel", CANCEL),
                     ("retry", RETRY), ("download", DOWNLOAD), ("projects", PROJECTS), ("status", STATUS)):
        if rx.search(t):
            return Intent(kind)
    if has_projects and (m := SWITCH.match(t)):
        target = m.group("target").strip().lower()
        number = _project_number(target, project_names or {})
        if number is not None:
            return Intent("switch", str(number))
    if has_projects and NOT_NEW.search(t) and CHANGE.search(t):
        return Intent("change", t)
    if NEW_APP.search(t) or NEW_SITE.search(t):
        return Intent("new", t)
    if short_site_request(t):
        return Intent("new", t)  # "Fitness website", "bakery site": enough to start (the style question follows)
    if QUESTION.search(t) and not (has_active and CHANGE.search(t) and REQUEST.search(t)):
        return Intent("question", t)
    if not has_projects:
        # Nothing to change yet: anything descriptive is an app idea.
        return Intent("new", t) if len(t) >= 15 and len(t.split()) >= 3 else Intent("unclear")
    if CHANGE.search(t):
        return Intent("change", t)  # the service asks which app when none is active
    if t.endswith("?") and len(t.split()) < 12:
        return Intent("unclear")
    if len(t.split()) >= 3:
        return Intent("ambiguous", t)
    return Intent("unclear")


def _project_number(target: str, names: dict[int, str]) -> int | None:
    words = {"one": 1, "first": 1, "1st": 1, "two": 2, "second": 2, "2nd": 2, "three": 3, "third": 3, "3rd": 3,
             "four": 4, "fourth": 4, "five": 5, "fifth": 5}
    target = target.strip(" .!?#")
    if target.isdigit():
        n = int(target)
        return n if n in names else None
    if target in words and words[target] in names:
        return words[target]
    if target in ("last", "latest", "newest", "recent", "previous") and names:
        return max(names)
    for n, name in names.items():
        nm = name.lower()
        if target and (target in nm or nm in target or target.replace(" ", "") in nm.replace(" ", "")):
            return n
    return None


def answer_reply(text: str) -> str | None:
    """Resolve a pending 'new app or change?' question. Returns 'change', 'new', 'no' or None."""
    t = text.strip()
    short = len(t.split()) <= 6
    if NEW_WORD.search(t) or (short and re.search(r"\bnew\b", t, re.I)):
        return "new"
    if YES.search(t) or (short and re.search(r"\bchange\b", t, re.I)):
        return "change"
    if NO.search(t):
        return "no"
    return None


def short_site_request(text: str) -> bool:
    """A few words naming what a website is for: "fitness website", "website for my bakery"."""
    words = re.findall(r"[\w']+", text.lower())
    if not 2 <= len(words) <= 6 or not re.search(r"\b(?:website|site|webpage|landing page)\b", text, re.I):
        return False
    if (QUESTION.search(text) and not REQUEST.search(text)) or CHANGE.search(text):
        return False
    return sum(1 for w in words if w not in FILLER and w not in SITE_WORDS) >= 1


def substance(text: str) -> int:
    """How many words of actual idea a message has ("i want a new app" has none)."""
    return sum(1 for w in re.findall(r"[\w']+", text.lower()) if w not in FILLER)
