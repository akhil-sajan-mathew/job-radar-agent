"""Telegram commands, read once at the start of each run with getUpdates (no server needed).

shortcut: replies wait for the next scheduled run, which can be hours away; a webhook (Cloudflare Worker that
starts the workflow) would answer in about a minute, see UPDATE_PLAN.md Step 3b.
Only messages from TELEGRAM_CHAT_ID are read; anything else is ignored.
"""
import logging
import os
import re
from datetime import datetime, timezone

import httpx

from .config import Config
from .cover import write_note
from .cv import render, tailor_cv
from .db import Db
from .notify import NL, Notifier
from .scorer import Scorer

log = logging.getLogger(__name__)
MAX_PER_RUN = 10  # bounds the Gemini calls a burst of /cover messages can trigger
HELP = NL.join([
    "Commands (answered at the next run, which can take a few hours):",
    "/cover #ref: cover note for that job",
    "/cv #ref: one-page PDF CV tailored to that job",
    "/why #ref: the full score breakdown",
    "/pause, /resume: stop or restart alerts (jobs keep being collected)",
    "/status: last run, Gemini and JSearch use, failing sources",
    "The #ref is the last line of each alert.",
])
_CMD = re.compile(r"^/([a-z]+)(?:@\w+)?(?:\s+#?([0-9a-f]{6,16}))?", re.I)


def fetch_commands(client: httpx.Client, token: str, chat_id: str, db: Db) -> list[str]:
    """New command texts from the owner's chat. The offset is saved in the DB, and Telegram only drops an
    update once a later call passes a higher offset, so a crashed run (DB not saved) re-reads them."""
    try:
        r = client.get(f"https://api.telegram.org/bot{token}/getUpdates",
                       params={"offset": int(db.get_meta("tg:offset") or 0), "timeout": 0})
        r.raise_for_status()
        updates = r.json()["result"]
    except (httpx.HTTPError, KeyError, ValueError) as e:  # never log the URL: it contains the bot token
        log.error("telegram getUpdates failed: %s", type(e).__name__)
        return []
    if updates:
        db.set_meta("tg:offset", str(updates[-1]["update_id"] + 1))
    msgs = [u.get("message") or {} for u in updates]
    texts = [m.get("text") or "" for m in msgs if str((m.get("chat") or {}).get("id")) == str(chat_id)]
    return [t for t in texts if t.startswith("/")][:MAX_PER_RUN]


def answer(text: str, cfg: Config, db: Db, scorer: Scorer | None, notifier: Notifier) -> None:
    reply = handle(text, cfg, db, scorer)
    if isinstance(reply, str):
        notifier.send(reply)
    elif not notifier.send_document(reply[1], reply[2], reply[0]):
        notifier.send("The CV was made but Telegram refused the file. Try /cv again.")


def handle(text: str, cfg: Config, db: Db, scorer: Scorer | None) -> str | tuple[str, str, bytes]:
    """A text reply, or (caption, filename, pdf bytes) for /cv."""
    m = _CMD.match(text.strip())
    cmd, ref = (m.group(1).lower(), (m.group(2) or "").lower()) if m else ("", "")
    if cmd == "pause":
        db.set_meta("paused", "1")
        return "⏸ Alerts paused. Jobs keep being collected and scored; /resume sends them."
    if cmd == "resume":
        db.set_meta("paused", "")
        return "▶️ Alerts resumed."
    if cmd == "status":
        return status(cfg, db, scorer)
    if cmd not in ("cover", "why", "cv"):
        return HELP
    if not ref:
        return f"Add the job's ref, e.g. /{cmd} #a1b2c3 (last line of each alert)."
    found = db.find_job(ref)
    if not found:
        return f"No job #{ref} found."
    job, state, reason = found
    if cmd == "why":
        return why(job, state, reason)
    if cmd == "cv":
        if not scorer:
            return "Tailored CVs need GEMINI_API_KEY."
        cv = tailor_cv(scorer, job, cfg.cover.budget)
        if not cv:
            return f"Couldn't tailor a CV for #{ref} that passes the fact check (or Gemini was busy). Try later."
        pdf = render(cv, os.environ.get("CV_NAME") or "[Your name]",
                     os.environ.get("CV_CONTACT") or "[phone | email | LinkedIn | GitHub]")
        slug = re.sub(r"[^A-Za-z0-9]+", "_", f"{job.company}_{job.title}").strip("_")[:60]
        return f"📄 CV tailored for {job.title} at {job.company}. Read it before sending.", f"CV_{slug}.pdf", pdf
    note = db.get_note(job.id)
    if not note:
        if not scorer:
            return "Cover notes need GEMINI_API_KEY."
        note = write_note(scorer, job, cfg.cover.budget, cfg.cover.research)
        if not note:
            return f"Couldn't write a note for #{ref} that passes the fact check (or Gemini was busy). Try later."
        db.save_note(job.id, note)
    thin = "" if job.description else f"{NL}(Written from the title only: this posting's text wasn't saved.)"
    return f"✍️ Cover note · {job.title} at {job.company}{NL}{NL}{note}{thin}"


def why(job, state: str, reason: str) -> str:
    head = f"#{job.id[:6]} · {job.title} at {job.company} · status: {state}"
    m = job.match
    if not m:
        return NL.join([head, f"Not scored. {reason}".strip(), job.url])
    return NL.join([
        head, f"Score: {m.score}/100 · {m.reason}",
        f"Experience: {m.experience_required} · Fresher-friendly: {'yes' if m.fresher_friendly else 'no'}",
        f"Visa/relocation: {m.visa_or_relocation}",
        f"Missing skills: {', '.join(m.missing_skills) or 'none listed'}", job.url,
    ])


def status(cfg: Config, db: Db, scorer: Scorer | None) -> str:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    lines = [f"Previous run: {db.get_meta('last_run') or 'unknown'} (UTC)",
             f"Alerts: {'paused' if db.get_meta('paused') == '1' else 'on'}"]
    if scorer:
        lines += [f"Gemini {model}: {scorer.used_today(model)}/{scorer.daily_limit} today"
                  for model in (scorer.model, scorer.fallback) if model]
    lines.append(f"JSearch: {db.usage('jsearch', month)}/{cfg.jsearch.monthly_limit} this month")
    failing = db.failing()
    lines.append("Failing sources: " + (", ".join(f"{s} ({n} runs)" for s, n in failing.items()) or "none"))
    return NL.join(lines)
