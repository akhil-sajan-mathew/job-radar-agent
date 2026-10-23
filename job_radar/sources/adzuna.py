import logging

from ..config import Config
from ..models import make_job, parse_dt
from .base import get_json

log = logging.getLogger(__name__)


class Adzuna:
    """India only (Adzuna has no UAE). One request per city, roles OR-ed into one query."""

    name = "adzuna"

    def __init__(self, cfg: Config, app_id: str, app_key: str, client):
        self.cfg, self.app_id, self.app_key, self.client, self.errors = cfg, app_id, app_key, client, 0

    def fetch(self):
        jobs = []
        cities = [c for c in self.cfg.locations.india.cities if "remote" not in c.lower()]
        for city in cities:
            try:
                data = get_json(
                    self.client, "https://api.adzuna.com/v1/api/jobs/in/search/1",
                    params={
                        "app_id": self.app_id, "app_key": self.app_key, "results_per_page": 50,
                        "what_or": " ".join(self.cfg.profile.target_roles), "where": city,
                        "max_days_old": self.cfg.filters.max_job_age_days,
                    },
                )
            except Exception as e:
                self.errors += 1
                log.warning("adzuna/%s failed: %s", city, e)
                continue
            for j in data.get("results", []):
                loc = (j.get("location") or {}).get("display_name", "")
                jobs.append(make_job(
                    self.name, str(j["id"]), j["title"], (j.get("company") or {}).get("display_name", ""),
                    f"{loc}, India", j["redirect_url"], j.get("description"), parse_dt(j.get("created")),
                    salary=str(int(j["salary_min"])) if j.get("salary_min") else None,
                ))
        return jobs
