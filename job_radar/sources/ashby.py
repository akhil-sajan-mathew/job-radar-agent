from ..models import make_job, parse_dt
from .base import BoardSource


class Ashby(BoardSource):
    name = "ashby"

    def url(self, token):
        return f"https://api.ashbyhq.com/posting-api/job-board/{token}"

    def parse(self, token, data):
        return [
            make_job(
                self.name, j["id"], j["title"], token, j.get("location") or "", j["jobUrl"],
                j.get("descriptionPlain") or j.get("descriptionHtml"),
                parse_dt(j.get("publishedAt")), remote=j.get("isRemote"),
            )
            for j in data.get("jobs", [])
            if j.get("isListed", True)
        ]
