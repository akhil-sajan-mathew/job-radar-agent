import csv

import pytest

from job_radar import eval as ev
from job_radar.models import Match, make_job


def test_metrics_and_report():
    pairs = [(True, 90), (True, 70), (True, 40), (False, 80), (False, 20)]
    catch, noise, alerts = ev.metrics(pairs, 65)
    assert (round(catch, 2), round(noise, 2), alerts) == (0.67, 0.33, 3)
    details = [(a, s, f"job{s}", "r") for a, s in pairs]
    text = ev.report(pairs, details, 65)
    assert "<- current" in text and "  40  job40" in text and "  80  job80" in text


class Src:
    name, errors = "lever", 0

    def fetch(self):
        return [make_job("lever", str(i), f"Graduate Engineer {i}", "Acme", "Dubai", "http://a", "Python", None)
                for i in range(5)] + [make_job("lever", "x", "Senior Manager", "Acme", "Dubai", "http://a", "", None)]


class FakeScorer:
    def __init__(self):
        self.calls = 0

    def score(self, job):
        self.calls += 1
        return Match(score=90 if job.title.endswith(("0", "1")) else 30, reason="r")


def test_sample_label_run_and_cache(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "build_sources", lambda *a, **k: [Src()])
    assert ev.sample(cfg, {}, None, 4, tmp_path) == 4  # the filtered "Senior Manager" never appears
    with pytest.raises(SystemExit):
        ev.sample(cfg, {}, None, 4, tmp_path)  # never overwrites labels
    with open(tmp_path / "labels.csv", newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert all("Senior" not in r["title"] for r in rows)
    for r in rows:
        r["label"] = "apply" if r["title"].endswith(("0", "1")) else "s"
    with open(tmp_path / "labels.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    sc = FakeScorer()
    out = ev.run(cfg, sc, out=tmp_path)
    assert "catches" in out and sc.calls == 4
    ev.run(cfg, sc, out=tmp_path)
    assert sc.calls == 4  # cached: the second report costs nothing
