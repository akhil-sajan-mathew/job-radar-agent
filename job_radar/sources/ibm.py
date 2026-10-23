import logging

from ..models import make_job, parse_dt
from .base import get_json

log = logging.getLogger(__name__)
URL = "https://www-api.ibm.com/search/api/v2"
COUNTRIES = ["United Arab Emirates", "India"]  # UAE first
# shortcut: IBM tags ~99% of UAE/India jobs "Professional" (experienced hires); only entry-level
# tags are fetched so a fresher is not flooded. Add "Professional" here to see everything.
LEVELS = ["Entry Level", "Internship"]
HEADERS = {"Origin": "https://www.ibm.com", "Referer": "https://www.ibm.com/careers/search"}


class IBM:
    """The search API behind ibm.com/careers. Undocumented and unofficial: it can change or
    start rejecting us at any time, which shows up as a failing source."""

    name = "ibm"

    def __init__(self, client):
        self.client, self.errors = client, 0

    def fetch(self):
        jobs = []
        for country in COUNTRIES:
            body = {
                "appId": "careers", "scopes": ["careers2"], "lang": "zz", "localeSelector": {},
                "sm": {"query": "", "lang": "zz"}, "size": 100, "from": 0,
                "query": {"bool": {"must": [{"term": {"field_keyword_05": country}},
                                        {"terms": {"field_keyword_18": LEVELS}}]}},
                "sort": [{"dcdate": {"order": "desc"}}],
                "_source": ["title", "url", "description", "field_keyword_08", "field_keyword_17",
                            "field_keyword_18", "field_keyword_19", "dcdate"],
            }
            try:
                data = get_json(self.client, URL, method="POST", json=body, headers=HEADERS)
                for h in data["hits"]["hits"]:
                    s = h["_source"]
                    jobs.append(make_job(
                        self.name, h["_id"], s["title"], "IBM", s.get("field_keyword_19") or country, s["url"],
                        f"{s.get('field_keyword_18') or ''}. {s.get('description') or ''}", parse_dt(s.get("dcdate")),
                    ))
            except Exception as e:
                self.errors += 1
                log.warning("ibm/%s failed: %s", country, e)
        return jobs
