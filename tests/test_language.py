"""Natural chat and languages: switching between English, Hindi, Telugu, Kannada and Tamil, translating Weave's own
messages (links and IDs untouched), the chat brain answering people in parallel, and the backup model race."""

import threading
import time
import types

import pytest
from test_accounts import MEMBER, acc, link, signup  # noqa: F401 - fixture
from test_sms import OWNER
from test_unified import FakeAI, u, with_brain  # noqa: F401 - fixture

from genesis.sms.brain import ChatBrain, ChatDecision
from genesis.sms.language import requested_language, script_language
from genesis.sms.translate import Translation, Translator


class FakeTranslateAI:
    """Pretends to translate: prefixes the language and keeps the markers, or drops them when told to."""

    def __init__(self, lose_markers=False):
        self.lose = lose_markers
        self.calls = 0

    def ask(self, system, user, schema, title="", timeout=None):
        self.calls += 1
        lang = "HINDI" if "Hindi" in system else "TELUGU" if "Telugu" in system else "OTHER"
        text = user.replace("[[0]]", "") if self.lose else user
        return Translation(text=f"<{lang}> {text}"), {}


def with_translator(env, ai=None):
    env.sms.service.translator = Translator(env.settings, ai or FakeTranslateAI(), env.store, synchronous=True)
    return env


# ---------- detection ----------
@pytest.mark.parametrize("text,code", [
    ("tum hindi me baat karo pls", "hi-Latn"), ("CAN U TALK IN HINDI", "hi"), ("speak telugu", "te"),
    ("telugu lo matladu", "te-Latn"), ("kannada alli maatadu", "kn-Latn"), ("tamil la pesu", "ta-Latn"),
    ("english please", "en"), ("తెలుగులో మాట్లాడు", "te"), ("go back to english", "en"),
])
def test_language_requests(text, code):
    assert requested_language(text) == code


@pytest.mark.parametrize("text", ["make a website in hindi for my shop", "hindi news website", "i love telugu movies",
                                  "what is hindi for hello"])
def test_mentioning_a_language_is_not_a_request(text):
    assert requested_language(text) is None


def test_scripts():
    assert [script_language(t) for t in ("నమస్కారం", "ನಮಸ್ಕಾರ", "வணக்கம்", "नमस्ते भाई", "hello")] == ["te", "kn", "ta", "hi", None]


# ---------- switching and translation ----------
def test_switching_language_is_instant_and_sticks(u):
    with_translator(u)
    u.text(OWNER, "tum hindi me baat karo")
    assert u.last() == "Theek hai! Ab se main Hindi mein baat karunga." and u.user()["language"] == "hi-Latn"
    u.text(OWNER, "Create a premium website for my coffee brand")
    assert u.last().startswith("<HINDI>")
    u.drain()
    done = u.last()
    assert done.startswith("<HINDI>") and "/preview/" in done  # the link survived translation
    u.text(OWNER, "english please")
    assert u.user()["language"] is None and "English" in u.last()


def test_native_scripts_reach_whatsapp_intact(u):
    u.text(OWNER, "can you speak hindi")
    assert u.last() == "ठीक है! अब से मैं हिंदी में बात करूँगा।"  # was stripped to "!" before


def test_plain_sms_stays_ascii(u):
    from genesis.sms import messages as M
    assert M.fit("Hi “Sam” — नमस्ते 👋") == 'Hi "Sam" -'
    assert M.fit("Hi “Sam” — नमस्ते 👋", unicode=True) == "Hi “Sam” — नमस्ते 👋"
    assert M.name_of({"name": "चाय की दुकान"}) == "चाय की दुकान"


def test_writing_in_a_script_switches_language(u):
    with_translator(u)
    u.text(OWNER, "నమస్కారం")
    assert u.user()["language"] == "te" and u.last().startswith("<TELUGU>")


def test_a_bad_translation_falls_back_to_english(u):
    with_translator(u, FakeTranslateAI(lose_markers=True))
    u.text(OWNER, "speak hindi")
    u.text(OWNER, "Create a premium website for my coffee brand")
    u.drain()
    assert "/preview/" in u.last() and not u.last().startswith("<HINDI>")


def test_brain_replies_skip_translation_and_know_the_name(acc):  # noqa: F811
    signup(acc)
    link(acc)
    with_brain(acc, ChatDecision(reply="Tumhara naam Sam hai!", action="none"))
    ai = FakeTranslateAI()
    with_translator(acc, ai)
    acc.store.update_user(acc.user(MEMBER)["id"], language="hi-Latn")
    acc.text(MEMBER, "What's my name")
    assert acc.last(MEMBER) == "Tumhara naam Sam hai!" and ai.calls == 0
    context = acc.ai.contexts[-1]
    assert "Their name: Sam" in context and "Reply language: Hindi, written in English (Latin) letters" in context


def test_rules_answer_the_name_when_the_ai_is_down(acc):  # noqa: F811
    signup(acc)
    link(acc)
    acc.text(MEMBER, "What's my name")
    assert acc.last(MEMBER).startswith("You're Sam!")


def test_untranslated_messages_go_out_after_a_minute(u):
    u.store.enqueue_sms("whatsapp:+1", "hello", None, status="localizing")
    assert u.store.claim_outbound() == []
    u.store.all("SELECT 1")  # same connection
    with u.store.tx() as c:
        c.execute("UPDATE messages SET updated_at = updated_at - 120 WHERE status = 'localizing'")
    assert [m["body"] for m in u.store.claim_outbound()] == ["hello"]


# ---------- speed ----------
def test_brain_answers_different_people_at_the_same_time(settings):
    brain = ChatBrain(settings, ai=None)
    first_running, release = threading.Event(), threading.Event()
    order, second_done = [], threading.Event()

    def slow():
        first_running.set()
        release.wait(5)
        order.append("a1")

    brain.submit(slow, key=1)
    first_running.wait(2)
    brain.submit(lambda: (order.append("b"), second_done.set()), key=2)
    brain.submit(lambda: order.append("a2"), key=1)
    assert second_done.wait(2)  # person 2 isn't stuck behind person 1
    release.set()
    deadline = time.monotonic() + 3
    while len(order) < 3 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert order == ["b", "a1", "a2"]  # person 1's messages stay in order


def test_slow_chat_model_is_raced_by_the_backup(settings):
    from genesis.core.gemini import GeminiClient

    class Models:
        def generate_content(self, model, contents, config):
            if model == "slow-model":
                time.sleep(3)
            return types.SimpleNamespace(text='{"text": "hi from ' + model + '"}', usage_metadata=None, candidates=[])

    settings.chat_model, settings.chat_fallback_model, settings.chat_hedge_seconds = "slow-model", "fast-model", 0.1
    client = GeminiClient(settings)
    client._client = types.SimpleNamespace(models=Models())
    from google.genai import types as _warm  # noqa: F401 - the first import alone takes seconds
    started = time.monotonic()
    out, meta = client.ask("system", "user", Translation)
    assert out.text == "hi from fast-model" and meta["model"] == "fast-model" and time.monotonic() - started < 2


# ---------- never silent while signing up ----------
def test_bare_code_links_and_reminders_are_sent(acc):  # noqa: F811
    acc.text(MEMBER, "hi")
    acc.text(MEMBER, "weave u there?")
    assert len(acc.all(MEMBER)) == 1
    with acc.store.tx() as c:
        c.execute("UPDATE unknown_senders SET last_reply_at = last_reply_at - 200")
    acc.text(MEMBER, "weave u there?")
    assert "I'm here!" in acc.last(MEMBER)
    signup(acc)
    code = acc.web_client.post("/api/account/phone-code", headers={"X-Weave": "1"}).json()["code"]
    acc.text(MEMBER, code.lower())
    assert "You're in, Sam!" in acc.last(MEMBER)


def test_a_model_out_of_quota_falls_through_to_the_next(settings):
    from genesis.core.gemini import GeminiClient

    class Quota(Exception):
        code = 429

    asked = []

    class Models:
        def generate_content(self, model, contents, config):
            asked.append(model)
            if model == "spent-model":
                raise Quota("429 RESOURCE_EXHAUSTED free_tier_requests")
            return types.SimpleNamespace(text='{"text": "ok"}', usage_metadata=None, candidates=[])

    settings.gemini_model = "auto"
    client = GeminiClient(settings)
    client._client = types.SimpleNamespace(models=Models())
    client._available_models = {"spent-model", "gemini-3.5-flash-lite"}
    client._model = "spent-model"
    out, meta = client._structured("system", "user", Translation)
    assert out.text == "ok" and meta["model"] == "gemini-3.5-flash-lite" and asked == ["spent-model",
                                                                                         "gemini-3.5-flash-lite"]


def test_glm_answers_first_and_gemini_takes_over_when_it_is_busy(tmp_path):
    from genesis.config import Settings
    from genesis.core.gemini import GeminiClient
    from genesis.core.glm import GlmError, RoutedModels

    s = Settings(data_dir=tmp_path, ai_provider="glm", glm_api_key="glm-test", gemini_api_key="gemini-test",
                 gemini_model="auto", chat_model="gemini-3.5-flash-lite", glm_fallback_models="",
                 website_fallback_models="gemini-2.5-flash")
    assert (s.chat_model, s.chat_fallback_model) == ("glm-4.7-flash", "gemini-3.5-flash-lite")
    assert s.website_fallback_models == "gemini-3.5-flash,gemini-2.5-flash"

    asked = []

    class Busy:
        def generate_content(self, model, contents, config):
            asked.append(model)
            raise GlmError(503, "UNAVAILABLE: GLM is overloaded (1305)")

    class Gemini:
        def generate_content(self, model, contents, config):
            asked.append(model)
            return types.SimpleNamespace(text='{"text": "from gemini"}', usage_metadata=None, candidates=[])

    client = GeminiClient(s)
    client._client = types.SimpleNamespace(models=RoutedModels(Busy(), Gemini()))
    client._available_models = {"glm-4.7-flash", "gemini-3.5-flash", "gemini-2.5-flash"}
    out, meta = client._structured("system", "user", Translation)
    assert out.text == "from gemini" and asked == ["glm-4.7-flash", "gemini-3.5-flash"]
