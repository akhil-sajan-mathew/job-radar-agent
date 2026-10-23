"""Job-board alert emails via IMAP (read-only: BODY.PEEK, mailbox opened readonly, never delete/send).

shortcut: per-sender URL patterns and the title/company/location layout are educated guesses
until real sample emails are added to tests/fixtures; when a sender's layout isn't recognised
we fall back to subject + first link instead of dropping the email.
"""
import email
import email.header
import imaplib
import logging
import re
from datetime import timedelta
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit

from ..db import Db
from ..models import Job, country_from_location, make_job

log = logging.getLogger(__name__)
MAX_EMAILS = 50
WINDOW_DAYS = 7

# sender domain -> (name, regex the job link must match)
SENDERS = {
    "linkedin.com": ("linkedin", re.compile(r"linkedin\.com/comm/jobs/view/")),
    "indeed.com": ("indeed", re.compile(r"cts\.indeed\.com/|indeed\.com/(rc/clk|viewjob|m/viewjob)")),
    "naukri.com": ("naukri", re.compile(r"naukri\.com/(jd/)?job-listings")),
    "glassdoor.com": ("glassdoor", re.compile(r"glassdoor\.[a-z.]+/partner/jobListing")),
    "naukrigulf.com": ("naukrigulf", re.compile(r"naukrigulf\.com/.*(jid|job)")),  # unverified: no sample yet
    "bayt.com": ("bayt", re.compile(r"bayt\.com/.*/jobs/")),  # unverified: no sample yet
    "gulftalent.com": ("gulftalent", re.compile(r"gulftalent\.com/.*/jobs/")),  # unverified: no sample yet
}
# Anchors on Indeed's redirect host that are chrome, not jobs
_GENERIC = {"", "view job", "learn more", "this is a bad match", "no", "yes", "edit profile", "help centre",
            "indeed", "privacy policy", "terms", "unsubscribe", "pause these emails", "apply now", "view"}
_RATING = re.compile(r"^\d(\.\d)?\s*★?$")


class _Items(HTMLParser):
    """Flattens HTML into ordered items: ('a', href, [text chunks inside the outermost <a>]) and ('t', text).

    LinkedIn wraps the whole job card in an <a> that contains another <a>, so nested anchors
    are folded into the outer one instead of replacing it.
    """

    def __init__(self):
        super().__init__()
        self.items: list[tuple] = []
        self._href = None
        self._chunks: list[str] = []
        self._depth = 0
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "a":
            if self._depth == 0:
                self._href, self._chunks = dict(attrs).get("href") or "", []
            self._depth += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag == "a" and self._depth:
            self._depth -= 1
            if self._depth == 0:
                self.items.append(("a", self._href, self._chunks))

    def handle_data(self, data):
        text = " ".join(data.split())
        if self._skip or not text:
            return
        (self._chunks if self._depth else self.items).append(text if self._depth else ("t", text))


def _canonical(url: str) -> str:
    """Strip tracking params. Indeed's whole redirect URL is the identity; Glassdoor's id is in the query."""
    u = urlsplit(url)
    if "indeed.com" in u.netloc:
        return url
    if "glassdoor" in u.netloc:
        m = re.search(r"jobListingId=(\d+)", u.query)  # the same job recurs in several digests
        return f"glassdoor:{m.group(1)}" if m else f"{u.netloc}{u.path}?{u.query}"
    return (f"{u.netloc}{u.path}".rstrip("/")) or url


def _glassdoor(url: str) -> tuple[str, timedelta | None]:
    """The emailed link is a one-time tracking link that expires; the job id is stable. The same
    link says how old the listing already was when the email went out (age6d / age3h / agejp = just posted)."""
    u = urlsplit(url)
    jid = re.search(r"jobListingId=(\d+)", u.query)
    stable = f"{u.scheme}://{u.netloc}/job-listing/?jl={jid.group(1)}" if jid else url
    a = re.search(r"-age(jp|\d+[dh])-", u.query)
    if not a or a.group(1) == "jp":
        return stable, timedelta(0) if a else None
    n = int(a.group(1)[:-1])
    return stable, timedelta(days=n) if a.group(1).endswith("d") else timedelta(hours=n)


def _html_body(msg: Message) -> str:
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
    return ""


def _after(items: list[tuple], i: int, n: int = 2) -> list[str]:
    """The next n plain-text items after items[i], stopping at the next link."""
    out: list[str] = []
    for it in items[i + 1:]:
        if it[0] == "a" or len(out) == n:
            break
        out.append(it[1])
    return out


def _split_company_location(text: str) -> tuple[str, str]:
    company, _, location = text.partition("·")
    return company.strip(), location.strip()


def _extract(name: str, items: list[tuple], i: int) -> tuple[str, str, str] | None:
    """(title, company, location) for the link at items[i], or None if it isn't a job card."""
    _, href, chunks = items[i]
    chunks = [c for c in chunks if c.lower() not in ("view", "view job")]
    if not chunks:
        return None
    if name == "linkedin":  # card: title, "Company · Location", button
        company, location = _split_company_location(chunks[1]) if len(chunks) > 1 else ("", "")
        return chunks[0], company, location
    if name == "glassdoor":  # card: [logo letter], company, [rating], title, location, ...
        c = [x for x in chunks if not _RATING.match(x) and len(x) > 1]
        if len(c) < 3:
            return None
        company = re.sub(r"^([A-Z])\1(?=[a-z])", r"\1", c[0])  # logo letter glued on: "SSterling"
        return c[1], company, c[2]
    if name == "naukri":  # card: title, company, [rating], location
        c = [x for x in chunks if not _RATING.match(x)]
        return (c[0], c[1], c[-1]) if len(c) >= 3 else None
    if name == "indeed":  # the title link is followed by company and location texts
        if chunks[0].lower() in _GENERIC:
            return None
        after = _after(items, i)
        return chunks[0], after[0] if after else "", after[1] if len(after) > 1 else ""
    return chunks[0], "", ""  # unverified senders: title only


def _locate(name: str, location: str, href: str, subject: str) -> str:
    """Make the location resolvable to a country: URL slug (Naukri), then the alert subject."""
    if country_from_location(location) != "OTHER":
        return location
    slug = href.rsplit("/", 1)[-1].replace("-", " ")
    for hint in (slug, subject):
        c = country_from_location(hint)
        if c != "OTHER":
            return ", ".join(x for x in (location, "India" if c == "IN" else "United Arab Emirates") if x)
    return location


def parse_message(msg: Message, message_id: str) -> list[Job]:
    """Never raises: on any failure returns the subject + first link as one job."""
    subject = str(email.header.make_header(email.header.decode_header(msg.get("Subject", ""))))
    sender_name, addr = parseaddr(msg.get("From", ""))
    domain = addr.rsplit("@", 1)[-1].lower()
    key = next((d for d in SENDERS if domain == d or domain.endswith("." + d)), None)
    if key is None:
        return []
    name, link_re = SENDERS[key]
    source = f"email:{name}"
    try:
        posted = parsedate_to_datetime(msg["Date"]) if msg["Date"] else None
    except (TypeError, ValueError):
        posted = None
    body = _html_body(msg)
    jobs: list[Job] = []
    first_link = ""
    try:
        p = _Items()
        p.feed(body)
        seen = set()
        for i, it in enumerate(p.items):
            if it[0] != "a" or not it[1].startswith("http"):
                continue
            first_link = first_link or it[1]
            if not link_re.search(it[1]) or _canonical(it[1]) in seen:
                continue
            found = _extract(name, p.items, i)
            if not found:
                continue
            title, company, location = found
            seen.add(_canonical(it[1]))
            link, job_posted = it[1], posted
            if name == "glassdoor":
                link, age = _glassdoor(it[1])
                job_posted = posted - age if posted and age is not None else posted
            jobs.append(make_job(source, _canonical(it[1]), title, company or sender_name,
                                 _locate(name, location, it[1], subject), link, subject, job_posted))
    except Exception as e:
        log.warning("email parser %s failed: %s", name, e)
        jobs = []
    if not jobs:  # unknown layout: still surface the email rather than lose it
        m = re.search(r"https?://[^\s\"'<>]+", body or "")
        link = first_link or (m.group(0) if m else "")
        if link:
            jobs = [make_job(source, message_id, subject, sender_name or name, subject, link, subject, posted)]
    return jobs


class EmailAlerts:
    name = "email"

    def __init__(self, user: str, password: str, db: Db, host: str = "imap.gmail.com"):
        self.user, self.password, self.db, self.host = user, password, db, host
        self.errors = 0
        self.processed: list[str] = []  # Message-IDs to mark once their jobs were delivered

    def _connect(self) -> imaplib.IMAP4:
        conn = imaplib.IMAP4_SSL(self.host)
        conn.login(self.user, self.password)
        conn.select("INBOX", readonly=True)
        return conn

    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        try:
            conn = self._connect()
        except Exception as e:  # type only: never log credentials
            self.errors += 1
            log.error("imap login/select failed: %s", type(e).__name__)
            return []
        try:
            # Gmail search syntax, so a busy personal inbox never crowds out the job-board mails
            senders = " OR ".join(SENDERS)
            _, data = conn.uid("search", None, "X-GM-RAW", f'"from:({senders}) newer_than:{WINDOW_DAYS}d"')
            for uid in data[0].split()[-MAX_EMAILS:]:
                try:
                    _, raw = conn.uid("fetch", uid, "(BODY.PEEK[])")
                    msg = email.message_from_bytes(raw[0][1])
                    mid = (msg.get("Message-ID") or f"uid:{uid.decode()}").strip()
                    if self.db.email_seen(mid):
                        continue
                    jobs += parse_message(msg, mid)
                    self.processed.append(mid)
                except Exception as e:
                    self.errors += 1
                    log.warning("email %s failed: %s", uid, e)
        except Exception as e:
            self.errors += 1
            log.error("imap fetch failed: %s", type(e).__name__)
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        return jobs
