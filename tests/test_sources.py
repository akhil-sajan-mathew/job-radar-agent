import httpx
import respx

from job_radar.db import Db
from job_radar.main import run
from job_radar.notify import Notifier
from job_radar.sources.adzuna import Adzuna
from job_radar.sources.ashby import Ashby
from job_radar.sources.greenhouse import Greenhouse
from job_radar.sources.jsearch import URL, JSearch
from job_radar.sources.lever import Lever

NOW = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()  # stays within max age
JS = {"data": {"cursor": None, "jobs": [{"job_id": "j1", "job_title": "Graduate Engineer", "employer_name": "Acme",
                "job_city": "Dubai", "job_country": "AE", "job_apply_link": "http://apply",
                "job_description": "<p>Python</p>", "job_posted_at_datetime_utc": NOW}]}}


@respx.mock
def test_greenhouse_lever_ashby_parse():
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").respond(json={"jobs": [{
        "id": 1, "title": "Engineer", "location": {"name": "Dubai"}, "absolute_url": "http://g",
        "content": "&lt;p&gt;Hello&lt;/p&gt;", "first_published": NOW, "company_name": "Acme"}]})
    respx.get("https://api.lever.co/v0/postings/acme").respond(json=[{
        "id": "l1", "text": "Dev", "categories": {"location": "Pune"}, "hostedUrl": "http://l",
        "descriptionPlain": "Hi", "createdAt": 1760000000000}])
    respx.get("https://api.ashbyhq.com/posting-api/job-board/acme").respond(json={"jobs": [
        {"id": "a1", "title": "Dev", "location": "Abu Dhabi", "jobUrl": "http://a", "isRemote": False,
         "publishedAt": NOW, "descriptionPlain": "x"},
        {"id": "a2", "title": "Hidden", "location": "Abu Dhabi", "jobUrl": "http://a", "isListed": False}]})
    c = httpx.Client()
    g, lv, a = Greenhouse(["acme"], c).fetch(), Lever(["acme"], c).fetch(), Ashby(["acme"], c).fetch()
    assert (g[0].country, g[0].description, g[0].company) == ("AE", "Hello", "Acme")
    assert (lv[0].country, lv[0].posted_at.year) == ("IN", 2025)
    assert len(a) == 1 and a[0].country == "AE"


@respx.mock
def test_board_failure_isolated():
    respx.get("https://api.lever.co/v0/postings/bad").respond(404)
    respx.get("https://api.lever.co/v0/postings/ok").respond(json=[])
    src = Lever(["bad", "ok"], httpx.Client())
    assert src.fetch() == [] and src.errors == 1


@respx.mock
def test_adzuna(cfg):
    route = respx.get("https://api.adzuna.com/v1/api/jobs/in/search/1").respond(json={"results": [{
        "id": 5, "title": "Dev", "company": {"display_name": "Z"}, "location": {"display_name": "Pune"},
        "redirect_url": "http://z", "description": "d", "created": NOW}]})
    jobs = Adzuna(cfg, "id", "key", httpx.Client()).fetch()
    assert route.call_count == 4 and jobs[0].country == "IN"  # "Remote India" skipped


@respx.mock
def test_jsearch_budget_and_schedule(cfg):
    route = respx.get(URL).respond(json=JS)
    db = Db(":memory:")
    warnings = []
    month = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m")

    js = JSearch(cfg, "k", httpx.Client(), db, warnings.append)
    jobs = js.fetch()
    assert route.call_count == 3 and db.usage("jsearch", month) == 3 and jobs[0].country == "AE"
    assert route.calls[0].request.url.params["date_posted"] == "3days"
    assert route.calls[0].request.url.params["language"] == "en"
    assert JSearch(cfg, "k", httpx.Client(), db, warnings.append).fetch() == []  # not due again
    assert route.call_count == 3

    db.add_usage("jsearch", month, 146)  # 149 used; next call hits 150 -> one warning
    JSearch(cfg, "k", httpx.Client(), db, warnings.append, force=True).fetch()
    assert len(warnings) == 1 and "150/200" in warnings[0]
    JSearch(cfg, "k", httpx.Client(), db, warnings.append, force=True).fetch()
    assert len(warnings) == 1  # warned once per month

    db.add_usage("jsearch", month, 100)  # over the 180 cap
    before = route.call_count
    JSearch(cfg, "k", httpx.Client(), db, warnings.append, force=True).fetch()
    assert route.call_count == before


@respx.mock
def test_jsearch_failure_still_counts_and_no_retry(cfg):
    route = respx.get(URL).respond(500)
    db = Db(":memory:")
    js = JSearch(cfg, "k", httpx.Client(), db, print)
    assert js.fetch() == [] and route.call_count == 3 and js.errors == 3


@respx.mock
def test_pipeline_dedupes_across_runs(cfg):
    respx.get(URL).respond(json=JS)
    respx.get("https://api.telegram.org/botT/getUpdates").respond(json={"ok": True, "result": []})
    tg = respx.post("https://api.telegram.org/botT/sendMessage").respond(json={"ok": True})
    db, client = Db(":memory:"), httpx.Client()
    env = {"RAPIDAPI_KEY": "k"}
    n = Notifier("T", "C", client)
    s1 = run(cfg, env, db, n, client, only="jsearch")
    s2 = run(cfg, env, db, n, client, only="jsearch")
    assert s1["notified"] == 1 and s2["notified"] == 0 and s2["already_seen"] == 1  # same id from 3 queries is 1 job
    assert tg.call_count == 1


@respx.mock
def test_failed_send_is_retried_next_run(cfg):
    respx.get(URL).respond(json=JS)
    respx.get("https://api.telegram.org/botT/getUpdates").respond(json={"ok": True, "result": []})
    tg = respx.post("https://api.telegram.org/botT/sendMessage").respond(500)
    db, client = Db(":memory:"), httpx.Client()
    n = Notifier("T", "C", client)
    assert run(cfg, {"RAPIDAPI_KEY": "k"}, db, n, client, only="jsearch")["notified"] == 0
    tg.respond(json={"ok": True})
    assert run(cfg, {"RAPIDAPI_KEY": "k"}, db, n, client, only="jsearch")["notified"] == 1
