from ..models import make_job, parse_dt
from .base import BoardSource


class Greenhouse(BoardSource):
    name = "greenhouse"

    def url(self, token):
        return f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"

    def params(self):
        return {"content": "true"}

    def parse(self, token, data):
        return [
            make_job(
                self.name, str(j["id"]), j["title"], j.get("company_name") or token,
                (j.get("location") or {}).get("name", ""), j["absolute_url"], j.get("content"),
                parse_dt(j.get("first_published") or j.get("updated_at")),
            )
            for j in data.get("jobs", [])
        ]
