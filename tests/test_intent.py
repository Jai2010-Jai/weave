"""Plain-English routing: what everyday messages mean, and the conversation around them."""

import pytest
from test_sms import OWNER, Env

from genesis.sms.intent import answer_reply, classify

NAMES = {1: "HabitPulse", 2: "Lift Log"}


@pytest.mark.parametrize("text,kind", [
    ("hi", "greeting"), ("Hey Genesis!", "greeting"), ("good morning", "greeting"),
    ("thanks!", "thanks"), ("ok cool", "thanks"), ("👍", "thanks"),
    ("what can you do?", "help"), ("how does this work", "help"),
    ("how's it going?", "status"), ("is it ready yet", "status"), ("any update?", "status"), ("how long", "status"),
    ("show me my apps", "projects"), ("my apps", "projects"),
    ("send me the link again", "download"), ("where's the apk", "download"), ("link pls", "download"),
    ("cancel it", "cancel"), ("stop the build", "cancel"), ("nvm", "cancel"),
    ("try again", "retry"), ("retry", "retry"),
    ("make the buttons bigger", "change"), ("add a settings screen", "change"), ("can you use a blue theme", "change"),
    ("build me a tip calculator that splits the bill", "new"), ("I want an app that tracks my water intake", "new"),
    ("new app: a pomodoro timer", "new"), ("an app for tracking my books", "new"),
    ("my sister keeps forgetting her medicine schedule", "ambiguous"),
])
def test_classify_with_an_active_app(text, kind):
    assert classify(text, has_projects=True, has_active=True, project_names=NAMES).kind == kind


@pytest.mark.parametrize("text,target", [
    ("switch to 2", "2"), ("open the lift log", "2"), ("go back to habitpulse", "1"), ("use the first one", "1"),
])
def test_switching_by_number_or_name(text, target):
    intent = classify(text, has_projects=True, has_active=True, project_names=NAMES)
    assert (intent.kind, intent.arg) == ("switch", target)


def test_first_message_is_an_idea_unless_too_vague():
    assert classify("a journal for my dreams", has_projects=False, has_active=False).kind == "new"
    assert classify("hmm", has_projects=False, has_active=False).kind == "unclear"
    assert classify("use a blue color", has_projects=True, has_active=True, project_names=NAMES).kind == "change"


def test_pending_answers():
    assert answer_reply("change") == "change" and answer_reply("yes do it") == "change"
    assert answer_reply("new one") == "new" and answer_reply("make a new app") == "new"
    assert answer_reply("nope") == "no" and answer_reply("what's the weather") is None


@pytest.fixture
def env(settings):
    return Env(settings)


def test_small_talk_never_starts_a_build(env):
    env.text(OWNER, "Build me a habit tracker with streaks")
    env.drain()
    for text in ("thanks!", "hi", "ok cool", "what can you do?"):
        env.text(OWNER, text)
    assert len(env.jobs()) == 1


def test_ambiguous_message_asks_then_follows_the_answer(env):
    env.text(OWNER, "Build me a habit tracker with streaks")
    env.drain()
    env.text(OWNER, "my sister keeps forgetting her medicine schedule")
    assert "change" in env.last() and "new" in env.last() and len(env.jobs()) == 1
    env.text(OWNER, "new")
    jobs = env.jobs()
    assert len(jobs) == 2 and jobs[-1]["kind"] == "new" and "medicine" in jobs[-1]["prompt"]


def test_plain_english_flow(env):
    env.text(OWNER, "I want an app that tracks how much water I drink")
    env.drain()
    env.text(OWNER, "send me the link")
    assert "/download/" in env.last()
    env.text(OWNER, "make the buttons bigger")
    assert env.jobs()[-1]["kind"] == "change"
    env.text(OWNER, "how's it going?")
    assert env.jobs()[-1]["ref"] in env.last()
    env.text(OWNER, "cancel it")
    assert "cancel" in env.last().lower() or "Stopping" in env.last()


def test_plural_updates_is_a_status_check():
    for text in ("updates?", "any updates", "any updates?"):
        assert classify(text, has_projects=True, has_active=True).kind == "status"
