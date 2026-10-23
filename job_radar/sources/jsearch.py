import logging
from datetime import datetime, timedelta, timezone

from ..config import Config
from ..db import Db, now_iso
from ..models import make_job, parse_dt
from .base import get_json

log = logging.getLogger(__name__)
URL = "https://jsearch.p.rapidapi.com/search-v2"  # API v5; the old /search path no longer exists


class JSearch:
    """RapidAPI JSearch, free plan (hard monthly cap). Every request is counted in SQLite."""

    name = "jsearch"

    def __init__(self, cfg: Config, key: str, client, db: Db, warn, force: bool = False):
        self.cfg, self.key, self.client, self.db, self.warn, self.force = cfg, key, client, db, warn, force
        self.errors = 0

    def _due(self) -> bool:
        last = self.db.get_meta("jsearch:last_run")
        if self.force or not last:
            return True
        age = datetime.now(timezone.utc) - datetime.fromisoformat(last)
        return age >= timedelta(hours=self.cfg.jsearch.min_hours_between_runs)

    def fetch(self):
        c = self.cfg.jsearch
        if not self._due():
            log.info("jsearch: not due yet, skipping")
            return []
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        jobs = []
        for q in c.queries:
            if self.db.usage(self.name, month) >= c.monthly_limit:
                log.warning("jsearch: monthly limit %d reached, not calling", c.monthly_limit)
                break
            used = self.db.add_usage(self.name, month)  # count before calling: failed calls may be billed too
            try:
                data = get_json(
                    self.client, URL, retry=False,  # a retry would burn quota unseen by the counter
                    headers={"X-RapidAPI-Key": self.key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"},
                    params={"query": q.query, "country": q.country, "date_posted": c.date_posted,
                            "language": "en", "num_pages": 1,
                            **({"job_requirements": c.job_requirements} if c.job_requirements else {})},
                )
            except Exception as e:
                self.errors += 1
                log.warning("jsearch query %r failed: %s", q.query, e)
                continue
            if used >= c.warn_at and self.db.get_meta("jsearch:warned") != month:
                self.warn(f"⚠️ JSearch quota: {used}/200 requests used in {month} (stops at {c.monthly_limit}).")
                self.db.set_meta("jsearch:warned", month)
            jobs += [self._job(j) for j in data["data"]["jobs"]]  # v5: {"data": {"jobs": [...], "cursor": ...}}
        self.db.set_meta("jsearch:last_run", now_iso())
        return jobs

    def _job(self, j: dict):
        loc = ", ".join(p for p in (j.get("job_city"), j.get("job_state"), j.get("job_country")) if p) or (
            j.get("job_location") or "")
        sal = j.get("job_min_salary")
        return make_job(
            self.name, j["job_id"], j["job_title"], j.get("employer_name") or "", loc,
            j.get("job_apply_link") or j.get("job_google_link") or "", j.get("job_description"),
            parse_dt(j.get("job_posted_at_datetime_utc")),
            salary=f"{sal}-{j.get('job_max_salary')} {j.get('job_salary_currency') or ''}".strip() if sal else None,
            remote=j.get("job_is_remote"),
        )
