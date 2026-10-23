from ..models import make_job, parse_dt
from .base import BoardSource


class Lever(BoardSource):
    name = "lever"

    def url(self, token):
        return f"https://api.lever.co/v0/postings/{token}"

    def params(self):
        return {"mode": "json"}

    def parse(self, token, data):
        return [
            make_job(
                self.name, j["id"], j["text"], token,
                (j.get("categories") or {}).get("location") or "", j["hostedUrl"],
                j.get("descriptionPlain") or j.get("description"), parse_dt(j.get("createdAt")),
            )
            for j in data
        ]
