import re
from datetime import datetime, timedelta, timezone

from .config import Config
from .models import Job

# "3+ years", "5-7 years", "minimum 4 yrs", "0 to 2 years": capture the lower bound
_YEARS = re.compile(r"(\d{1,2})\s*(?:\+|(?:-|–|to)\s*\d{1,2})?\s*(?:years?|yrs?)\b", re.I)
_PREFERRED = re.compile(r"\s*(?:of\s+\w+\s+)?(?:experience\s+)?(?:is\s+)?(?:preferred|a plus|desirable)", re.I)


# Alert emails only have a title, so the experience check is blind: demand a fresher signal instead
_FRESHER = re.compile(
    r"\b(graduate|intern|internship|trainee|fresher|freshers|junior|jr|entry|associate|apprentice|campus)\b", re.I
)


def min_years_required(text: str) -> int | None:
    """Smallest experience lower bound mentioned near the word 'experience', else None."""
    mins = []
    for m in _YEARS.finditer(text):
        window = text[max(0, m.start() - 60): m.end() + 60].lower()
        if "experience" not in window and "exp" not in window:
            continue  # "founded 10 years ago" etc.
        if _PREFERRED.match(text[m.end(): m.end() + 40]):
            continue
        mins.append(int(m.group(1)))
    return min(mins) if mins else None


def reject_reason(job: Job, cfg: Config, now: datetime | None = None) -> str | None:
    """Lenient pre-filter: returns why a job is dropped, or None to keep it."""
    now = now or datetime.now(timezone.utc)
    title = job.title.lower()
    if any(k.lower() in title for k in cfg.filters.exclude_keywords):
        return "exclude keyword in title"
    if job.country == "OTHER":
        return "country not AE/IN"
    if job.posted_at and now - job.posted_at > timedelta(days=cfg.filters.max_job_age_days):
        return "too old"
    years = min_years_required(job.description)
    if years is not None and years > cfg.profile.experience_max_years:
        return f"requires {years}+ years"
    haystack = f"{title} {job.description.lower()}"
    wanted = cfg.filters.include_keywords + cfg.profile.target_roles
    if not any(re.search(r"\b" + re.escape(k.lower()) + r"s?\b", haystack) for k in wanted):  # whole words
        return "no include keyword"
    if cfg.filters.email_requires_fresher_title and job.source.startswith("email:") and not _FRESHER.search(title):
        return "email job without fresher signal in title"  # emails carry no description to check experience
    return None


DIRECT = {"greenhouse", "lever", "ashby", "smartrecruiters", "emirates", "ibm"}  # company-owned links


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def fuzzy_duplicates(jobs: list[Job], known: list[tuple[str, str, str]]) -> tuple[list[Job], list[Job]]:
    """Split jobs into (kept, duplicates): same country, same normalized company, title similarity >= 90
    against a known (company, title, country) or an earlier kept job. Among duplicates within the batch the
    most direct link (company site / ATS over aggregator over email) wins."""
    from rapidfuzz import fuzz

    def rank_of(j: Job) -> int:
        return 0 if j.source in DIRECT else 2 if j.source.startswith("email:") else 1

    def same(a: tuple[str, str, str], b: tuple[str, str, str]) -> bool:
        return a[2] == b[2] and _norm(a[0]) == _norm(b[0]) and fuzz.ratio(_norm(a[1]), _norm(b[1])) >= 90

    kept: list[Job] = []
    dups: list[Job] = []
    for j in sorted(jobs, key=rank_of):
        key = (j.company, j.title, j.country)
        if any(same(key, k) for k in known) or any(same(key, (k.company, k.title, k.country)) for k in kept):
            dups.append(j)
        else:
            kept.append(j)
    return kept, dups
