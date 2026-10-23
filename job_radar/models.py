import hashlib
import html
import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

Country = Literal["AE", "IN", "OTHER"]

_AE = re.compile(
    r"dubai|abu dhabi|sharjah|ajman|ras al.khaimah|fujairah|umm al.quwain|al ain"
    r"|united arab emirates|\buae\b"
)
_IN = re.compile(
    r"\bindia\b|bengaluru|bangalore|hyderabad|pune|chennai|mumbai|delhi|gurgaon|gurugram"
    r"|noida|kolkata|ahmedabad|kochi|trivandrum|thiruvananthapuram|coimbatore|jaipur"
)


def country_from_location(location: str) -> Country:
    loc = location.lower()
    if _AE.search(loc):
        return "AE"
    if _IN.search(loc):
        return "IN"
    return "OTHER"  # bare "Remote" lands here on purpose


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", html.unescape(text))  # Greenhouse sends escaped HTML
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_dt(value: str | int | float | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):  # Lever: epoch milliseconds
        return datetime.fromtimestamp(value / 1000, timezone.utc)
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Match(BaseModel):
    """The scoring model's verdict on one job."""

    score: int = Field(ge=0, le=100)
    reason: str
    experience_required: str = "not stated"
    fresher_friendly: bool = True
    visa_or_relocation: str = "not stated"
    missing_skills: list[str] = []


class Job(BaseModel):
    id: str
    source: str
    title: str
    company: str
    location: str
    country: Country
    url: str
    description: str
    posted_at: datetime | None
    fetched_at: datetime
    salary: str | None = None
    remote: bool | None = None
    match: Match | None = None  # set by the scorer


def make_job(
    source: str,
    source_id: str,
    title: str,
    company: str,
    location: str,
    url: str,
    description: str | None,
    posted_at: datetime | None,
    salary: str | None = None,
    remote: bool | None = None,
) -> Job:
    key = f"{source}:{source_id}" if source_id else f"{company}|{title}|{location}".lower()
    return Job(
        id=hashlib.sha1(key.encode()).hexdigest()[:16],
        source=source,
        title=title.strip(),
        company=company.strip(),
        location=location.strip(),
        country=country_from_location(location),
        url=url,
        description=strip_html(description)[:6000],
        posted_at=posted_at,
        fetched_at=datetime.now(timezone.utc),
        salary=salary,
        remote=remote,
    )
