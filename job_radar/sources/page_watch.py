import hashlib
import logging
import re
from datetime import datetime, timezone

from ..config import PageWatch
from ..db import Db
from ..models import make_job, strip_html

log = logging.getLogger(__name__)


class PageWatcher:
    """Alerts when a page's text changes; the first sight only records a baseline.
    shortcut: digits are ignored so counters and dates don't fire every run (a changed closing
    date is missed); plain HTTP only, so sites that block non-browser clients just fail."""

    name = "pagewatch"

    def __init__(self, pages: list[PageWatch], client, db: Db):
        self.pages, self.client, self.db, self.errors = pages, client, db, 0

    def fetch(self):
        jobs = []
        for pg in self.pages:
            try:
                r = self.client.get(pg.url)
                r.raise_for_status()
                body = re.sub(r"(?is)<(script|style).*?</\1>", " ", r.text)
                text = re.sub(r"\d+", "", strip_html(body)).lower()
            except Exception as e:
                self.errors += 1
                log.warning("pagewatch/%s failed: %s", pg.name, e)
                continue
            digest = hashlib.sha256(text.encode()).hexdigest()
            key = f"page:{pg.url}"
            old = self.db.get_meta(key)
            self.db.set_meta(key, digest)
            if old and old != digest:
                jobs.append(make_job(
                    self.name, f"{pg.url}#{digest[:12]}", f"Page changed: {pg.name}", pg.name, pg.location, pg.url,
                    "graduate programme or careers page changed, check it", datetime.now(timezone.utc)))
        return jobs
