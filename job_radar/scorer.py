import json
import logging
import re
import time

import httpx
from pydantic import ValidationError

from .models import Job, Match

log = logging.getLogger(__name__)
URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
PACE = 4.5  # free tier is ~15 requests/minute (check AI Studio for your project's real limit)

PROMPT = """You screen job postings for a fresh graduate. Reply with JSON only.

CANDIDATE RESUME:
{resume}

JOB:
Title: {title}
Company: {company}
Location: {location}
Description (may be missing or only a short snippet): {description}

Score 0-100 how worthwhile it is for this candidate to apply:
- role and skill match to the resume;
- experience required must fit a fresh graduate with 0 years: penalize roles needing 3+ years heavily;
  do NOT penalize when experience is not stated, and do not invent requirements missing from the text;
- for UAE roles, whether a non-local candidate appears eligible (visa/relocation wording).

JSON keys: score (integer 0-100), reason (one short sentence), experience_required (e.g. "0-2 years" or
"not stated"), fresher_friendly (boolean), visa_or_relocation ("sponsors" | "local candidates only" |
"not stated"), missing_skills (list of short strings)."""


def parse_score(text: str) -> Match:
    """Validate the model's reply; tolerates a ```json fence."""
    return Match.model_validate_json(re.sub(r"^```(?:json)?|```$", "", text.strip()).strip())


class Scorer:
    def __init__(self, key: str, model: str, resume: str, client: httpx.Client, fallback: str = ""):
        self.key, self.model, self.resume, self.client, self.fallback = key, model, resume, client, fallback
        self.tokens = 0  # logged per run so cost/quota use is visible

    def _call(self, prompt: str, model: str) -> str:
        r = self.client.post(
            URL.format(model=model), headers={"x-goog-api-key": self.key}, timeout=60,
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"responseMimeType": "application/json", "temperature": 0}},
        )
        r.raise_for_status()
        data = r.json()
        self.tokens += (data.get("usageMetadata") or {}).get("totalTokenCount", 0)
        return data["candidates"][0]["content"]["parts"][0]["text"]

    def score(self, job: Job) -> Match | None:
        """Up to 3 tries (429/503/bad JSON); None means scoring failed."""
        prompt = PROMPT.format(resume=self.resume, title=job.title, company=job.company,
                               location=job.location, description=job.description[:3500] or "(none)")
        # Free-tier capacity errors (503) are common: retry the primary once after a pause, then the fallback model
        for attempt, model in enumerate([self.model, self.model, self.fallback], 1):
            if not model:
                break
            time.sleep(PACE)
            try:
                return parse_score(self._call(prompt, model))
            except (httpx.HTTPError, ValidationError, KeyError, IndexError, json.JSONDecodeError) as e:
                status = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else ""
                log.warning("scoring %r failed (try %d, %s): %s %s", job.title, attempt, model, type(e).__name__, status)  # noqa: E501
                time.sleep(15 if attempt == 1 else 5)
        return None
