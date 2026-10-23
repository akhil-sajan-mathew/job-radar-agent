import httpx
import respx

from job_radar.config import PageWatch
from job_radar.db import Db
from job_radar.sources.emirates import SITE, Emirates
from job_radar.sources.ibm import IBM
from job_radar.sources.ibm import URL as IBM_URL
from job_radar.sources.page_watch import PageWatcher
from job_radar.sources.smartrecruiters import SmartRecruiters

SR = "https://api.smartrecruiters.com/v1/companies/EtihadAirways5/postings"


def sr_item(i, city="Abu Dhabi"):
    return {"id": str(i), "name": f"Graduate Engineer {i}", "company": {"name": "Etihad Airways"},
            "releasedDate": "2026-10-22T13:12:01.288Z", "experienceLevel": {"label": "Entry Level"},
            "location": {"fullLocation": f"{city}, , United Arab Emirates", "remote": False}}


@respx.mock
def test_smartrecruiters_pages_and_location():
    route = respx.get(SR).mock(side_effect=[
        httpx.Response(200, json={"totalFound": 3, "content": [sr_item(1), sr_item(2)]}),
        httpx.Response(200, json={"totalFound": 3, "content": [sr_item(3)]})])
    jobs = SmartRecruiters(["EtihadAirways5"], httpx.Client()).fetch()
    assert route.call_count == 2 and len(jobs) == 3
    assert jobs[0].location == "Abu Dhabi, United Arab Emirates" and jobs[0].country == "AE"
    assert jobs[0].url == "https://jobs.smartrecruiters.com/EtihadAirways5/1" and "Entry Level" in jobs[0].description


@respx.mock
def test_smartrecruiters_failure_isolated():
    respx.get(SR).respond(404)
    src = SmartRecruiters(["EtihadAirways5"], httpx.Client())
    assert src.fetch() == [] and src.errors == 1


@respx.mock
def test_emirates_feed():
    feed = {"data": [{"reqid": "24885", "title": "Graduate Engineer", "brand": "Emirates", "city": "Dubai",
                      "country": "United Arab Emirates", "category": "Information Technology",
                      "jobdescription": "<p>0-2 years</p>", "postingdate": 1792108800000}]}
    respx.get(f"{SITE}/api/v1/jobs").respond(json=feed)
    jobs = Emirates(httpx.Client()).fetch()
    assert jobs[0].country == "AE" and jobs[0].company == "Emirates" and jobs[0].posted_at.year == 2026
    assert "0-2 years" in jobs[0].description and "<p>" not in jobs[0].description


@respx.mock
def test_ibm_search_api_posts_levels_per_country():
    hit = {"_id": "abc", "_source": {"title": "Software Intern", "url": "https://careers.ibm.com/careers/JobDetail?jobId=1",
                                     "description": "Join us", "field_keyword_18": "Internship",
                                     "field_keyword_19": "ABU DHABI, AE", "dcdate": "2026-10-20"}}
    route = respx.post(IBM_URL).respond(json={"hits": {"hits": [hit]}})
    jobs = IBM(httpx.Client()).fetch()
    assert route.call_count == 2  # UAE then India
    assert b"Internship" in route.calls[0].request.content and b"United Arab Emirates" in route.calls[0].request.content
    assert jobs[0].country == "AE" and jobs[0].company == "IBM" and jobs[0].posted_at.day == 20


@respx.mock
def test_ibm_failure_counts():
    respx.post(IBM_URL).respond(403)
    src = IBM(httpx.Client())
    assert src.fetch() == [] and src.errors == 2


@respx.mock
def test_page_watch_baseline_then_change():
    page = respx.get("https://example.com/grad")
    pw = PageWatcher([PageWatch(name="Grad programme", url="https://example.com/grad")], httpx.Client(), Db(":memory:"))
    page.respond(text="<html><body>Applications open 12 Nov 2026 <script>x=1</script></body></html>")
    assert pw.fetch() == []  # baseline
    page.respond(text="<html><body>Applications open 99 Nov 2030 <script>x=2</script></body></html>")
    assert pw.fetch() == []  # digits and scripts ignored: no noise
    page.respond(text="<html><body>Applications closed. New intake announced</body></html>")
    (job,) = pw.fetch()
    assert job.title == "Page changed: Grad programme" and job.country == "AE"
    assert pw.fetch() == []


@respx.mock
def test_page_watch_blocked_site_is_an_error():
    respx.get("https://example.com/x").respond(403)
    pw = PageWatcher([PageWatch(name="X", url="https://example.com/x")], httpx.Client(), Db(":memory:"))
    assert pw.fetch() == [] and pw.errors == 1
