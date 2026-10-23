from datetime import datetime, timedelta, timezone

import pytest

from job_radar.config import Config, load_config
from job_radar.db import Db
from job_radar.filter import min_years_required, reject_reason
from job_radar.models import country_from_location, make_job, strip_html
from job_radar.notify import format_job, rank


@pytest.mark.parametrize("loc,want", [
    ("Dubai - United Arab Emirates", "AE"), ("UAE", "AE"), ("Abu Dhabi", "AE"),
    ("Bengaluru, Karnataka", "IN"), ("Remote India", "IN"), ("Pune", "IN"),
    ("Remote", "OTHER"), ("London", "OTHER"), ("", "OTHER"),
])
def test_country(loc, want):
    assert country_from_location(loc) == want


@pytest.mark.parametrize("text,want", [
    ("0-2 years of experience", 0), ("1+ years experience", 1), ("minimum 3 years experience", 3),
    ("5-7 years of experience required", 5), ("2+ years experience preferred", None),
    ("founded 10 years ago", None), ("no requirement", None), ("0 to 2 years experience", 0),
])
def test_experience(text, want):
    assert min_years_required(text) == want


def job(**kw):
    d = dict(source="x", source_id="1", title="Software Engineer", company="Acme", location="Dubai",
             url="http://a", description="Python role", posted_at=datetime.now(timezone.utc))
    return make_job(**{**d, **kw})


def test_filter(cfg):
    assert reject_reason(job(), cfg) is None
    assert reject_reason(job(title="Senior Software Engineer"), cfg)
    assert reject_reason(job(location="London"), cfg)
    assert reject_reason(job(posted_at=datetime.now(timezone.utc) - timedelta(days=30)), cfg) == "too old"
    assert reject_reason(job(description="engineer, 4+ years of experience"), cfg)
    assert reject_reason(job(description="engineer, 0-2 years of experience"), cfg) is None
    assert reject_reason(job(title="Chef", description="cook"), cfg) == "no include keyword"
    assert reject_reason(job(posted_at=None), cfg) is None  # unknown date is kept


def test_job_id_stable_and_html_stripped():
    assert job().id == job().id and job().id != job(source_id="2").id
    assert strip_html("&lt;p&gt;Hi&nbsp;<b>there</b>&lt;/p&gt;") == "Hi there"
    assert len(job(description="a" * 9000).description) == 6000


def test_db_seen_usage_and_dry(tmp_path):
    db = Db(tmp_path / "s.db")
    j = job()
    assert not db.seen(j.id)
    db.save(j, "notified")
    assert db.seen(j.id)
    assert db.add_usage("jsearch", "2026-10") == 1 and db.add_usage("jsearch", "2026-10") == 2
    assert Db(tmp_path / "s.db").usage("jsearch", "2026-10") == 2  # persists
    dry = Db(tmp_path / "d.db", dry=True)
    dry.save(j, "notified")
    dry.set_meta("k", "v")
    assert not dry.seen(j.id) and dry.get_meta("k") is None
    assert dry.add_usage("jsearch", "2026-10") == 1  # quota is always counted


def test_health_streak():
    db = Db(":memory:")
    assert [db.record_health("s", ok) for ok in (False, False, False, True)] == [1, 2, 3, 0]


def test_format_and_rank():
    now = datetime.now(timezone.utc)
    uae = job(posted_at=now - timedelta(hours=2), source="greenhouse")
    ind = job(source_id="2", location="Pune", posted_at=now)
    assert rank([ind, uae])[0] is uae
    text = format_job(uae, now)
    assert "🇦🇪 Software Engineer" in text and "Posted 2h ago · via Greenhouse" in text and "Apply: http://a" in text


def test_config_validation(tmp_path):
    bad = tmp_path / "c.yaml"
    bad.write_text("profile: {}")
    with pytest.raises(SystemExit):
        load_config(bad)
    c = load_config("config.yaml")
    assert isinstance(c, Config)
    data = c.model_dump()
    data["jsearch"]["queries"] *= 2  # 6 queries
    with pytest.raises(ValueError):
        Config.model_validate(data)


def test_env_file_loader(tmp_path, monkeypatch):
    from job_radar.main import load_env_file
    f = tmp_path / ".env"
    f.write_text('# c\nA_KEY=abc\nB_KEY=\nC_KEY="q v"\nA_SET=file\n')
    monkeypatch.setenv("A_SET", "real")
    for k in ("A_KEY", "B_KEY", "C_KEY"):
        monkeypatch.delenv(k, raising=False)
    load_env_file(str(f))
    import os
    assert (os.environ["A_KEY"], os.environ["C_KEY"], os.environ["A_SET"]) == ("abc", "q v", "real")
    assert "B_KEY" not in os.environ  # empty values stay unset


def test_summary_stays_under_telegram_limit_and_only_marks_listed():
    import httpx
    import respx

    from job_radar.notify import SUMMARY_CHARS, Notifier
    jobs = [job(source_id=str(i), title=f"Engineer {i}", url="http://x/" + "a" * 300) for i in range(60)]
    with respx.mock:
        route = respx.post("https://api.telegram.org/botT/sendMessage").respond(json={"ok": True})
        sent = Notifier("T", "C", httpx.Client()).notify_jobs(jobs)
    texts = [__import__("json").loads(c.request.content)["text"] for c in route.calls]
    assert all(len(t) <= 4096 for t in texts)
    assert len(sent) == 10 + len(texts[-1].split("\n\n")) - 1 < 60  # individually sent + those listed; rest retried
    assert "rest next run" in texts[-1] and SUMMARY_CHARS < 4096


def test_email_jobs_need_fresher_title_and_intern_is_a_word(cfg):
    assert reject_reason(job(source="email:glassdoor", title="Full Stack Developer"), cfg)
    assert reject_reason(job(source="email:glassdoor", title="Full Stack Developer Internship"), cfg) is None
    assert reject_reason(job(source="email:linkedin", title="Graduate Software Engineer"), cfg) is None
    assert reject_reason(job(source="jsearch", title="Full Stack Developer"), cfg) is None  # has a real description
    assert reject_reason(job(title="International Sales Rep", description="sell"), cfg) == "no include keyword"


def test_fuzzy_dedupe_prefers_direct_link_and_skips_already_notified():
    from job_radar.filter import fuzzy_duplicates
    agg = job(source_id="1", source="jsearch", title="Graduate Software Engineer", company="Acme Corp.")
    direct = job(source_id="2", source="greenhouse", title="Graduate Software Engineer ", company="acme corp")
    other_country = job(source_id="3", source="jsearch", title="Graduate Software Engineer",
                        company="Acme Corp", location="Pune")
    different = job(source_id="4", source="jsearch", title="Marketing Manager", company="Acme Corp")
    kept, dups = fuzzy_duplicates([agg, direct, other_country, different], [])
    assert {j.id for j in kept} == {direct.id, other_country.id, different.id} and dups == [agg]
    kept, dups = fuzzy_duplicates([agg], [("Acme Corp", "Graduate Software Engineer", "AE")])
    assert kept == [] and dups == [agg]
