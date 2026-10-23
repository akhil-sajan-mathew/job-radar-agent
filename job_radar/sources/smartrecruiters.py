import logging

from ..models import make_job, parse_dt
from .base import get_json

log = logging.getLogger(__name__)
MAX_PAGES = 5


class SmartRecruiters:
    """Public Posting API (no key). The list endpoint has no description, so only the
    experience-level label is searchable; shortcut: fetch /postings/{id} if that proves too thin."""

    name = "smartrecruiters"

    def __init__(self, tokens: list[str], client):
        self.tokens, self.client, self.errors = tokens, client, 0

    def fetch(self):
        jobs = []
        for token in self.tokens:
            try:
                offset = 0
                for _ in range(MAX_PAGES):
                    data = get_json(self.client, f"https://api.smartrecruiters.com/v1/companies/{token}/postings",
                                    params={"limit": 100, "offset": offset})
                    for j in data["content"]:
                        level = (j.get("experienceLevel") or {}).get("label", "")
                        loc = j.get("location") or {}
                        jobs.append(make_job(
                            self.name, f"{token}/{j['id']}", j["name"], (j.get("company") or {}).get("name", token),
                            ", ".join(x for x in loc.get("fullLocation", "").split(", ") if x), f"https://jobs.smartrecruiters.com/{token}/{j['id']}",
                            f"Experience level: {level}", parse_dt(j.get("releasedDate")), remote=loc.get("remote"),
                        ))
                    offset += len(data["content"])
                    if not data["content"] or offset >= data.get("totalFound", 0):
                        break
            except Exception as e:
                self.errors += 1
                log.warning("smartrecruiters/%s failed: %s", token, e)
        return jobs
