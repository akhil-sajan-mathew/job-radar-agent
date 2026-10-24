import logging
import time
from datetime import datetime, timezone

import httpx

from .models import Job

log = logging.getLogger(__name__)
PACE = 1.0  # Telegram allows ~1 msg/sec per chat
FLAG = {"AE": "🇦🇪", "IN": "🇮🇳", "OTHER": "🌍"}
MAX_INDIVIDUAL = 10
SUMMARY_CHARS = 3500
NL = chr(10)  # f-strings can't hold a backslash before Python 3.12


def ago(dt: datetime | None, now: datetime) -> str:
    if not dt:
        return "Posted date unknown"
    h = int((now - dt).total_seconds() // 3600)
    return f"Posted {h}h ago" if h < 48 else f"Posted {h // 24}d ago"


def format_job(job: Job, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    m = job.match
    head = f"{FLAG[job.country]} {m.score}/100 · {job.title}" if m else f"{FLAG[job.country]} {job.title}"
    lines = [head, f"{job.company} · {job.location}", f"{ago(job.posted_at, now)} · via {job.source.capitalize()}"]
    if m:
        lines.append(f"{'✅ Fresher-friendly' if m.fresher_friendly else '⚠️ Not fresher-friendly'}"
                     f" · Experience: {m.experience_required} · Visa: {m.visa_or_relocation}")
        lines.append(f"Why: {m.reason}")
        if m.missing_skills:
            lines.append(f"Missing: {', '.join(m.missing_skills)}")
    if job.flags:
        lines.append("⚠️ Posting contains hidden or instruction-like text: treat its score with caution")
    return NL.join(lines + [f"Apply: {job.url}", f"#{job.id[:6]}"])  # the ref for /cover and /why


def rank(jobs: list[Job]) -> list[Job]:
    """UAE before India; within each, best score first, then newest."""
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    jobs = sorted(jobs, key=lambda j: j.posted_at or epoch, reverse=True)
    return sorted(jobs, key=lambda j: (j.country != "AE", -(j.match.score if j.match else 0)))


class Notifier:
    def __init__(self, token: str, chat_id: str, client: httpx.Client, dry: bool = False):
        self.token, self.chat_id, self.client, self.dry = token, chat_id, client, dry

    def send(self, text: str) -> bool:
        if self.dry:
            print(f"--- would send ---\n{text}\n")
            return True
        try:
            r = self.client.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True},
            )
            r.raise_for_status()
        except httpx.HTTPError as e:  # never log the URL: it contains the bot token
            log.error("telegram send failed: %s", type(e).__name__)
            return False
        time.sleep(PACE)
        return True

    def send_document(self, filename: str, data: bytes, caption: str = "") -> bool:
        if self.dry:  # written next to you instead; *.pdf is gitignored
            open(filename, "wb").write(data)
            print(f"--- would send file {filename} ({len(data)} bytes): {caption} ---")
            return True
        try:
            r = self.client.post(f"https://api.telegram.org/bot{self.token}/sendDocument",
                                 data={"chat_id": self.chat_id, "caption": caption[:1000]},
                                 files={"document": (filename, data, "application/pdf")})
            r.raise_for_status()
        except httpx.HTTPError as e:  # never log the URL: it contains the bot token
            log.error("telegram file send failed: %s", type(e).__name__)
            return False
        time.sleep(PACE)
        return True

    def notify_jobs(self, jobs: list[Job]) -> list[Job]:
        """Send the top 10 individually plus one summary; returns the jobs actually delivered."""
        jobs = rank(jobs)
        sent = [j for j in jobs[:MAX_INDIVIDUAL] if self.send(format_job(j))]
        rest = jobs[MAX_INDIVIDUAL:]
        listed, lines, size = [], [], 0
        for j in rest:  # Telegram caps a message at 4096 chars; the rest waits for the next run
            score = f"{j.match.score}/100 · " if j.match else ""
            line = f"{FLAG[j.country]} {score}{j.title} · {j.company} · {j.location} · #{j.id[:6]}{NL}{j.url}"
            if size + len(line) > SUMMARY_CHARS:
                break
            listed.append(j)
            lines.append(line)
            size += len(line) + 2
        if listed:
            more = f" ({len(listed)} listed, rest next run)" if len(listed) < len(rest) else ""
            head = f"+{len(rest)} more matches{more}"
            if self.send(f"{head}:{NL}{NL}" + (NL * 2).join(lines)):
                sent += listed
        return sent
