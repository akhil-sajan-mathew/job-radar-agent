import logging

from ..models import make_job, parse_dt
from .base import get_json

log = logging.getLogger(__name__)
SITE = "https://www.emiratesgroupcareers.com"


class Emirates:
    """The JSON feed behind emiratesgroupcareers.com's own search page (undocumented, first-party).
    shortcut: the feed has no per-job link, so every alert links to the search page."""

    name = "emirates"

    def __init__(self, client):
        self.client, self.errors = client, 0

    def fetch(self):
        try:
            data = get_json(self.client, f"{SITE}/api/v1/jobs",
                            params={"title": "", "brand": "", "category": "", "location": "",
                                    "jobcategory": "", "showAll": "true"})
            return [
                make_job(self.name, str(j["reqid"]), j["title"], j.get("brand") or "Emirates Group",
                         ", ".join(x for x in (j.get("city"), j.get("country")) if x),
                         f"{SITE}/search-and-apply/", f"{j.get('category') or ''}. {j.get('jobdescription') or ''}",
                         parse_dt(j.get("postingdate")))
                for j in data["data"]
            ]
        except Exception as e:
            self.errors += 1
            log.warning("emirates failed: %s", e)
            return []
