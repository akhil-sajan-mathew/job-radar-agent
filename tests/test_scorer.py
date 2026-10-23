import json
from collections import Counter
from datetime import datetime, timezone

import httpx
import pytest
import respx
from pydantic import ValidationError

from job_radar.db import Db
from job_radar.main import score_jobs
from job_radar.models import Match, make_job
from job_radar.notify import format_job, rank
from job_radar.scorer import URL, Scorer, parse_score

GOOD = {"score": 82, "reason": "Strong Python/SQL match; 0-1 yrs required.", "experience_required": "0-1 years",
        "fresher_friendly": True, "visa_or_relocation": "not stated", "missing_skills": ["Docker"]}
ENDPOINT = URL.format(model="m")


def reply(payload):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}],
                                     "usageMetadata": {"totalTokenCount": 100}})


def job(i="1", country_city="Dubai", title="Graduate Engineer"):
    return make_job("jsearch", i, title, "Acme", country_city, f"http://a/{i}", "Python role",
                    datetime.now(timezone.utc))


def test_parse_score_validates():
    assert parse_score("```json\n" + json.dumps(GOOD) + "\n```").score == 82
    for bad in ('{"score": 150, "reason": "x"}', "not json", '{"reason": "no score"}'):
        with pytest.raises(ValidationError):
            parse_score(bad)


@respx.mock
def test_scorer_success_and_token_log():
    route = respx.post(ENDPOINT).mock(return_value=reply(GOOD))
    s = Scorer("k", "m", "resume text", httpx.Client())
    m = s.score(job())
    assert m.score == 82 and s.tokens == 100
    sent = route.calls[0].request
    assert sent.headers["x-goog-api-key"] == "k" and b"resume text" in sent.content  # key in header, never the URL
    assert "k" not in str(sent.url).split("?")[-1]


@respx.mock
def test_scorer_retries_then_falls_back_then_gives_up():
    main = respx.post(ENDPOINT).mock(side_effect=[httpx.Response(503), httpx.Response(503)])
    fb = respx.post(URL.format(model="fb")).mock(return_value=reply(GOOD))
    assert Scorer("k", "m", "r", httpx.Client(), "fb").score(job()).score == 82
    assert main.call_count == 2 and fb.call_count == 1  # primary twice, then the fallback model
    main.mock(side_effect=[reply("garbage")] * 2)
    fb.mock(return_value=reply("still garbage"))
    assert Scorer("k", "m", "r", httpx.Client(), "fb").score(job()) is None


class FakeScorer:
    tokens = 0

    def __init__(self, results):
        self.results, self.calls = results, 0

    def score(self, job):
        self.calls += 1
        r = self.results[job.title]
        return None if r is None else Match(score=r, reason="because")


def run_scoring(jobs, results, db=None, cap=60, threshold=65):
    from job_radar.config import load_config
    cfg = load_config("config.yaml")
    cfg.scoring.max_jobs_scored_per_run, cfg.scoring.notify_threshold = cap, threshold
    stats, db = Counter(), db or Db(":memory:")
    sc = FakeScorer(results)
    keep, incomplete = score_jobs(jobs, cfg, db, sc, stats)
    return keep, incomplete, stats, db, sc


def test_threshold_suppresses_and_records_low_scores():
    hi, lo = job("1", title="Hi"), job("2", title="Lo")
    keep, incomplete, stats, db, _ = run_scoring([hi, lo], {"Hi": 80, "Lo": 40})
    assert [j.title for j in keep] == ["Hi"] and keep[0].match.score == 80 and not incomplete
    assert stats["low_score"] == 1 and db.seen(lo.id) and not db.seen(hi.id)


def test_cache_never_scores_twice():
    j = job("1", title="Hi")
    db = Db(":memory:")
    _, _, _, _, sc = run_scoring([j], {"Hi": 80}, db)
    keep, _, _, _, sc2 = run_scoring([j], {"Hi": 80}, db)
    assert sc.calls == 1 and sc2.calls == 0 and keep[0].match.score == 80


def test_cap_defers_rest_to_next_run():
    jobs = [job(str(i), title=f"J{i}") for i in range(3)]
    keep, incomplete, stats, _, sc = run_scoring(jobs, {f"J{i}": 90 for i in range(3)}, cap=2)
    assert sc.calls == 2 and len(keep) == 2 and incomplete and stats["deferred"] == 1


def test_failed_score_uae_notified_india_retried():
    uae, ind = job("1", "Dubai", "U"), job("2", "Pune", "I")
    keep, incomplete, stats, _, _ = run_scoring([uae, ind], {"U": None, "I": None})
    assert keep == [uae] and keep[0].match is None and incomplete and stats["score_failed"] == 2


def test_format_with_match_and_rank_by_score():
    a, b = job("1", title="A"), job("2", title="B")
    a.match, b.match = Match(score=70, reason="ok"), Match(score=90, reason="great", missing_skills=["Docker"])
    assert rank([a, b])[0] is b
    text = format_job(b)
    assert "🇦🇪 90/100 · B" in text and "Why: great" in text and "Missing: Docker" in text
    assert "Fresher-friendly" in text and "Apply: http://a/2" in text
