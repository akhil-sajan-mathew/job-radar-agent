import json
from collections import Counter

import httpx
import respx

from job_radar.cover import ungrounded, write_note
from job_radar.db import Db
from job_radar.main import send_notes
from job_radar.models import Match, make_job
from job_radar.notify import Notifier
from job_radar.scorer import URL, Scorer

RESUME = "B.Tech CS 2026. Python, PyTorch, Next.js, FastAPI. ManTacAi: 96.8% accuracy. AI/ML projects."
ENDPOINT = URL.format(model="m")
CLEAN = ("I am a 2026 B.Tech graduate who builds with Python and PyTorch. My ManTacAi project reached "
         "96.8% accuracy. Your AI team at Acme is where I want to apply that.")
INVENTED = CLEAN + " I also led a Kubernetes migration that cut costs by 40%."


def reply(text):
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})


def js(**kw):
    return reply(json.dumps(kw))


def job(score=85, title="AI Engineer"):
    j = make_job("lever", title, title, "Acme", "Dubai", "http://a", "Python and AI role at Acme.", None)
    j.match = Match(score=score, reason="fit")
    return j


def scorer(db=None, limit=500):
    return Scorer("k", "m", RESUME, httpx.Client(), "", db or Db(":memory:"), daily_limit=limit)


def test_ungrounded_finds_invented_names_and_numbers_only():
    assert ungrounded(CLEAN, RESUME, "Acme", "AI Engineer") == []
    assert ungrounded(INVENTED, RESUME, "Acme") == ["40", "Kubernetes"]
    assert ungrounded("I know AWS.", RESUME, "Good laws apply") == ["AWS"]  # whole words, not substrings
    assert ungrounded("Python\nUsed PyTorch daily", RESUME) == []  # a new line starts a sentence (CV bullets)


@respx.mock
def test_clean_draft_passes_in_one_round_with_research():
    route = respx.post(ENDPOINT).mock(side_effect=[
        reply("Acme builds AI tools for airlines."),
        js(note=CLEAN, claims=["Python"]),
        js(verdict="ok", edits=[], unsupported=[]),
    ])
    assert write_note(scorer(), job(), 400) == CLEAN and route.call_count == 3
    assert json.loads(route.calls[0].request.content)["tools"] == [{"google_search": {}}]


@respx.mock
def test_invented_metric_is_caught_and_revised():
    route = respx.post(ENDPOINT).mock(side_effect=[
        js(note=INVENTED, claims=[]),
        js(verdict="ok", edits=[], unsupported=[]),  # the reviewer missed it; the code check didn't
        js(note=CLEAN, claims=[]),
    ])
    assert write_note(scorer(), job(), 400, with_research=False) == CLEAN and route.call_count == 3
    assert "Kubernetes" in json.loads(route.calls[2].request.content)["contents"][0]["parts"][0]["text"]


@respx.mock
def test_still_ungrounded_after_revision_is_never_sent():
    respx.post(ENDPOINT).mock(side_effect=[
        js(note=INVENTED, claims=[]),
        js(verdict="revise", edits=["shorter"], unsupported=["Kubernetes"]),
        js(note=INVENTED, claims=[]),
    ])
    assert write_note(scorer(), job(), 400, with_research=False) is None


@respx.mock
def test_research_failure_does_not_stop_the_note():
    respx.post(ENDPOINT).mock(side_effect=[
        httpx.Response(503), httpx.Response(503),  # research: primary twice, no fallback configured
        js(note=CLEAN, claims=[]),
        js(verdict="ok", edits=[], unsupported=[]),
    ])
    assert write_note(scorer(), job(), 400) == CLEAN


@respx.mock
def test_budget_leaves_room_for_scoring():
    route = respx.post(ENDPOINT).mock(return_value=js(note=CLEAN, claims=[]))
    db = Db(":memory:")
    s = scorer(db)
    db.add_usage("gemini:m", __import__("job_radar.scorer", fromlist=["x"]).today(), 400)
    assert write_note(s, job(), 400) is None and route.call_count == 0


def test_send_notes_only_for_strong_new_jobs(cfg, monkeypatch):
    written = []
    monkeypatch.setattr("job_radar.main.write_note", lambda s, j, *a: written.append(j.title) or "Note")
    db, stats = Db(":memory:"), Counter()
    n = Notifier("T", "C", httpx.Client(), dry=True)
    strong, weak, unscored, done = job(90, "Strong"), job(70, "Weak"), job(99, "Unscored"), job(95, "Done")
    unscored.match = None
    db.save_note(done.id, "old note")
    send_notes([weak, strong, unscored, done], cfg, db, scorer(), n, stats)
    assert written == ["Strong"] and stats["notes"] == 1 and db.get_note(strong.id) == "Note"
