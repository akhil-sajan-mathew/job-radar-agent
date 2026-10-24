import email
import re
from email.message import EmailMessage
from pathlib import Path

import httpx
import respx

from job_radar.db import Db
from job_radar.main import run
from job_radar.notify import Notifier
from job_radar.sources.email_alerts import EmailAlerts, parse_message

FIX = Path(__file__).parent / "fixtures"


def fixture(name):
    return email.message_from_bytes((FIX / f"{name}.eml").read_bytes())


def msg(sender="jobs-noreply@linkedin.com", subject="New jobs: Dubai", html="<p>hi</p>", mid="<m1@x>"):
    m = EmailMessage()
    m["From"], m["Subject"], m["Message-ID"], m["Date"] = sender, subject, mid, "Tue, 20 Oct 2026 10:00:00 +0000"
    m.set_content(html, subtype="html")
    return m


def rows(name):
    return [(j.title, j.company, j.country) for j in parse_message(fixture(name), "<x>")]


def test_linkedin_fixture():
    jobs = parse_message(fixture("linkedin"), "<x>")
    assert rows("linkedin") == [
        ("Associate Product Analyst - Intern", "Leadrat Real Estate CRM - Dubai | India", "IN"),
        ("Data Science", "EXL", "IN")]  # nested <a> card: title, "Company · Location", View button
    assert jobs[0].source == "email:linkedin" and jobs[0].url.endswith("/jobs/view/4473092770/")
    assert jobs[0].posted_at.year == 2026


def test_naukri_fixture():
    assert rows("naukri") == [
        ("Data Annotator", "Mosigma It Solution...", "IN"),  # location "Remote" -> India from the URL slug
        ("Customer Support Executive WFH SS", "NoBroker", "IN")]


def test_indeed_fixture_single_job_not_chrome_links():
    assert rows("indeed") == [("Senior AI Developer / Full-Stack Vibe Coder", "XTRA", "AE")]


def test_glassdoor_fixture():
    r = rows("glassdoor")
    assert len(r) == 13 and r[0] == ("Full Stack Cloud Engineer", "Sterling Workforce Enterprises", "IN")
    assert ("Python Backend Engineer", "Techolution", "IN") in r
    assert len({j.id for j in parse_message(fixture("glassdoor"), "<x>")}) == 13  # query-string ids differ


def test_fixtures_contain_no_personal_info():
    for f in FIX.glob("*.eml"):
        assert not re.search(r"akhil|asajan|mathew", f.read_text(encoding="utf-8", errors="ignore"), re.I)


def test_fallback_to_subject_and_first_link():
    html = '<a href="https://click.example.com/x">Open</a>'
    jobs = parse_message(msg(html=html), "<m1@x>")
    assert len(jobs) == 1 and jobs[0].title == "New jobs: Dubai" and jobs[0].url.endswith("/x")
    assert jobs[0].country == "AE"


def test_unknown_sender_ignored_and_garbage_safe():
    assert parse_message(msg(sender="a@evil.com"), "<x>") == []
    assert parse_message(msg(html="<<<not html"), "<x>") == []  # no link at all: nothing, no crash


RAW = (FIX / "linkedin.eml").read_bytes()
MID = "<fixture-linkedin@example.com>"


class FakeImap:
    def __init__(self, raw):
        self.raw, self.calls = raw, []

    def uid(self, cmd, *a):
        self.calls.append((cmd, a))
        if cmd == "search":
            return "OK", [b"1"]
        return "OK", [(b"1", self.raw)]

    def logout(self):
        pass


def test_fetch_uses_peek_and_skips_seen(monkeypatch):
    fake = FakeImap(RAW)
    db = Db(":memory:")
    src = EmailAlerts("u", "p", db)
    monkeypatch.setattr(src, "_connect", lambda: fake)
    assert len(src.fetch()) == 2 and src.processed == [MID]
    assert fake.calls[1][1][1] == "(BODY.PEEK[])"  # read-only, doesn't set \Seen
    q = fake.calls[0][1]  # search only the job-board senders, not the whole inbox
    assert q[1] == "X-GM-RAW" and "from:(linkedin.com OR indeed.com" in q[2] and "newer_than:7d" in q[2]
    db.mark_email(MID)
    src2 = EmailAlerts("u", "p", db)
    monkeypatch.setattr(src2, "_connect", lambda: fake)
    assert src2.fetch() == []


def test_login_failure_counts_error():
    src = EmailAlerts("u", "p", Db(":memory:"))
    src._connect = lambda: (_ for _ in ()).throw(OSError("boom"))
    assert src.fetch() == [] and src.errors == 1


@respx.mock
def test_pipeline_marks_email_only_after_delivery(cfg, monkeypatch):
    tg = respx.post("https://api.telegram.org/botT/sendMessage").respond(500)
    respx.get("https://api.telegram.org/botT/getUpdates").respond(json={"ok": True, "result": []})
    cfg.filters.max_job_age_days = 100000  # the fixture emails have fixed dates
    db, client = Db(":memory:"), httpx.Client()
    n = Notifier("T", "C", client)
    monkeypatch.setattr(EmailAlerts, "_connect", lambda self: FakeImap(RAW))
    env = {"IMAP_USER": "u", "IMAP_APP_PASSWORD": "p"}
    assert run(cfg, env, db, n, client, only="email")["notified"] == 0
    assert not db.email_seen(MID)  # send failed -> will be re-read
    tg.respond(json={"ok": True})
    assert run(cfg, env, db, n, client, only="email")["notified"] == 1  # "Data Science": no fresher signal
    assert db.email_seen(MID)
    assert run(cfg, env, db, n, client, only="email")["notified"] == 0


def test_glassdoor_same_job_in_different_digests_is_one_job():
    from job_radar.sources.email_alerts import _canonical
    a = "https://www.glassdoor.co.in/partner/jobListing.htm?pos=104&guid=AAA&jobListingId=1010297835559&cs=1"
    b = "https://www.glassdoor.co.in/partner/jobListing.htm?pos=101&guid=BBB&jobListingId=1010297835559&cs=9"
    assert _canonical(a) == _canonical(b)


def test_glassdoor_link_is_stable_and_age_comes_from_the_listing_not_the_email():
    from datetime import timedelta

    from job_radar.sources.email_alerts import _glassdoor
    base = "https://www.glassdoor.co.in/partner/jobListing.htm?pos=1&jobListingId=1010297835559&utm_content="
    link, age = _glassdoor(base + "ja-jobpos1-age6d-1010297835559")
    assert link == "https://www.glassdoor.co.in/job-listing/?jl=1010297835559" and age == timedelta(days=6)
    assert _glassdoor(base + "ja-jobpos1-agejp-1")[1] == timedelta(0)
    assert _glassdoor(base + "ja-jobpos1-age5h-1")[1] == timedelta(hours=5)
    assert _glassdoor(base + "junk")[1] is None
    jobs = parse_message(fixture("glassdoor"), "<x>")
    assert jobs[0].url.endswith("?jl=1010297835559") and "utm" not in jobs[0].url
    assert (jobs[0].fetched_at - jobs[0].posted_at).days >= 6  # age6d on top of the email's own date
