import logging
import time

import httpx

from ..models import Job

log = logging.getLogger(__name__)
PACE = 0.5  # seconds between requests
USER_AGENT = "job-radar/0.1 (personal job alert tool)"


def new_client() -> httpx.Client:
    return httpx.Client(timeout=15, headers={"User-Agent": USER_AGENT}, follow_redirects=True)


def get_json(client: httpx.Client, url: str, retry: bool = True, method: str = "GET", **kwargs) -> dict | list:
    """GET (or `method`) with one retry on network errors, 429 and 5xx."""
    for attempt in (1, 2):
        time.sleep(PACE)
        try:
            r = client.request(method, url, **kwargs)
            if r.status_code == 429 or r.status_code >= 500:
                raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError:
            if attempt == 2 or not retry:
                raise
            time.sleep(2)
    raise AssertionError("unreachable")


class BoardSource:
    """Company ATS boards: one URL per token; a failing board is logged and skipped."""

    name = ""

    def __init__(self, tokens: list[str], client: httpx.Client):
        self.tokens, self.client, self.errors = tokens, client, 0

    def url(self, token: str) -> str:
        raise NotImplementedError

    def params(self) -> dict:
        return {}

    def parse(self, token: str, data) -> list[Job]:
        raise NotImplementedError

    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        for token in self.tokens:
            try:
                jobs += self.parse(token, get_json(self.client, self.url(token), params=self.params()))
            except Exception as e:  # isolate per board: bad token or changed format
                self.errors += 1
                log.warning("%s/%s failed: %s", self.name, token, e)
        return jobs
