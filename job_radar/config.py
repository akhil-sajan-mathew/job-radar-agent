from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError


class Profile(BaseModel):
    target_roles: list[str]
    skills: list[str]
    experience_max_years: int = 1
    resume_path: str = "resume.md"


class Region(BaseModel):
    priority: int
    cities: list[str]


class Locations(BaseModel):
    uae: Region
    india: Region


class Filters(BaseModel):
    include_keywords: list[str]
    exclude_keywords: list[str]
    max_job_age_days: int = 7
    email_requires_fresher_title: bool = True


class Scoring(BaseModel):
    model: str = "gemini-3.1-flash-lite"  # Google AI Studio free tier; any generateContent model name works
    fallback_model: str = "gemini-3.5-flash-lite"  # tried when the main model is overloaded
    notify_threshold: int = 65
    max_jobs_scored_per_run: int = 60
    daily_limit: int = 500  # free-tier requests/day per model (AI Studio > Rate Limit)
    warn_at: int = 450


class Cover(BaseModel):
    enabled: bool = True
    min_score: int = 80
    max_per_run: int = 3
    research: bool = False  # one Google-Search-grounded call per note; not on the free tier
    budget: int = 400  # notes stop at this many requests/day per model, leaving the rest for scoring


class Telegram(BaseModel):
    webhook: bool = False  # true once the Cloudflare relay is set up: Telegram then refuses getUpdates


class Companies(BaseModel):
    greenhouse: list[str] = []
    lever: list[str] = []
    ashby: list[str] = []
    smartrecruiters: list[str] = []


class Portals(BaseModel):
    emirates: bool = False
    ibm: bool = False


class PageWatch(BaseModel):
    name: str
    url: str
    location: str = "United Arab Emirates"  # country hint for the alert


class JSearchQuery(BaseModel):
    query: str
    country: str


class JSearch(BaseModel):
    queries: list[JSearchQuery] = Field(max_length=3)
    date_posted: str = "3days"
    job_requirements: str = ""  # JSearch filter, e.g. "no_experience,under_3_years_experience"
    min_hours_between_runs: int = 10
    monthly_limit: int = 180
    warn_at: int = 150


class Config(BaseModel):
    profile: Profile
    locations: Locations
    filters: Filters
    scoring: Scoring = Scoring()
    cover: Cover = Cover()
    telegram: Telegram = Telegram()
    companies: Companies = Companies()
    jsearch: JSearch
    portals: Portals = Portals()
    page_watch: list[PageWatch] = []


def load_config(path: str | Path = "config.yaml") -> Config:
    try:
        return Config.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as e:
        raise SystemExit(f"Invalid config {path}:\n{e}") from e
