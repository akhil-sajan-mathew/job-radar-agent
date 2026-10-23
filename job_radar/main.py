import argparse
import logging
import os
from collections import Counter

from .config import Config, load_config
from .db import Db
from .filter import fuzzy_duplicates, reject_reason
from .notify import Notifier, rank
from .scorer import Scorer
from .sources.adzuna import Adzuna
from .sources.ashby import Ashby
from .sources.base import new_client
from .sources.email_alerts import EmailAlerts
from .sources.emirates import Emirates
from .sources.greenhouse import Greenhouse
from .sources.ibm import IBM
from .sources.jsearch import JSearch
from .sources.lever import Lever
from .sources.page_watch import PageWatcher
from .sources.smartrecruiters import SmartRecruiters

log = logging.getLogger("job_radar")
HEALTH_WARN_STREAK = 3


def build_sources(cfg: Config, env, client, db: Db, warn, only: str | None = None) -> list:
    """Sources whose keys are available (ATS boards need none). `only` forces a single source."""
    sources = [
        Greenhouse(cfg.companies.greenhouse, client),
        Lever(cfg.companies.lever, client),
        Ashby(cfg.companies.ashby, client),
        SmartRecruiters(cfg.companies.smartrecruiters, client),
    ]
    if cfg.portals.emirates:
        sources.append(Emirates(client))
    if cfg.portals.ibm:
        sources.append(IBM(client))
    if cfg.page_watch:
        sources.append(PageWatcher(cfg.page_watch, client, db))
    if env.get("ADZUNA_APP_ID") and env.get("ADZUNA_APP_KEY"):
        sources.append(Adzuna(cfg, env["ADZUNA_APP_ID"], env["ADZUNA_APP_KEY"], client))
    else:
        log.warning("adzuna disabled: ADZUNA_APP_ID/ADZUNA_APP_KEY not set")
    if env.get("RAPIDAPI_KEY"):
        sources.append(JSearch(cfg, env["RAPIDAPI_KEY"], client, db, warn, force=only == "jsearch"))
    else:
        log.warning("jsearch disabled: RAPIDAPI_KEY not set")
    if env.get("IMAP_USER") and env.get("IMAP_APP_PASSWORD"):
        sources.append(EmailAlerts(env["IMAP_USER"], env["IMAP_APP_PASSWORD"], db))
    else:
        log.warning("email alerts disabled: IMAP_USER/IMAP_APP_PASSWORD not set")
    return [s for s in sources if only in (None, s.name)]


def run(cfg: Config, env, db: Db, notifier: Notifier, client, dry: bool = False, only: str | None = None,
        scorer: Scorer | None = None) -> Counter:
    stats: Counter = Counter()
    batch: dict[str, object] = {}
    processed: list[str] = []
    for src in build_sources(cfg, env, client, db, notifier.send, only):
        try:
            jobs = src.fetch()
        except Exception as e:  # one broken source never stops the run
            log.error("%s failed: %s", src.name, e)
            jobs, src.errors = [], 1
        stats[f"fetched:{src.name}"] += len(jobs)
        if src.name != "jsearch" or jobs or src.errors:  # a skipped (not due) JSearch isn't a health signal
            streak = db.record_health(src.name, src.errors == 0)
            if streak == HEALTH_WARN_STREAK:
                notifier.send(f"⚠️ Source '{src.name}' has failed {streak} runs in a row.")
        stats["errors"] += src.errors
        processed += getattr(src, "processed", [])
        for j in jobs:
            batch.setdefault(j.id, j)

    to_notify = []
    for job in batch.values():
        if db.seen(job.id):
            stats["already_seen"] += 1
        elif (reason := reject_reason(job, cfg)):
            db.save(job, "filtered", reason)
            stats["filtered"] += 1
        else:
            to_notify.append(job)
    to_notify, dups = fuzzy_duplicates(to_notify, db.notified())
    for job in dups:
        db.save(job, "duplicate", "same company/title/country already seen on another source")
        stats["duplicate"] += 1
    incomplete = False  # jobs left unscored for the next run: their emails must stay unmarked
    if scorer:
        to_notify, incomplete = score_jobs(to_notify, cfg, db, scorer, stats)
    sent = notifier.notify_jobs(to_notify)
    for job in sent:
        db.save(job, "notified", "score_failed" if scorer and not job.match else "")
        stats["notified"] += 1
    if len(sent) == len(to_notify) and not incomplete:  # else emails stay unmarked, re-read and retried
        for mid in processed:
            db.mark_email(mid)
    # jobs that failed to send stay unsaved, so the next run retries them
    return stats


def score_jobs(jobs, cfg: Config, db: Db, scorer: Scorer, stats: Counter):
    """Returns (jobs worth notifying, whether some were left for the next run).
    Over the per-run cap or a failed India score: left unsaved and retried. A failed UAE score
    is notified unscored (a false positive beats a missed job)."""
    keep, incomplete, calls = [], False, 0
    for job in rank(jobs):  # UAE first, newest first
        m = db.get_match(job.id)
        if m is None:
            if calls >= cfg.scoring.max_jobs_scored_per_run:
                stats["deferred"] += 1
                incomplete = True
                continue
            calls += 1
            m = scorer.score(job)
            if m:
                db.save_match(job.id, m)
        if m is None:
            stats["score_failed"] += 1
            if job.country == "AE":
                keep.append(job)
            else:
                incomplete = True
            continue
        job.match = m
        if m.score >= cfg.scoring.notify_threshold:
            keep.append(job)
        else:
            db.save(job, "low_score", f"{m.score}: {m.reason}")
            stats["low_score"] += 1
    log.info("scoring: %d calls, %d tokens", calls, scorer.tokens)
    return keep, incomplete


def build_scorer(cfg: Config, env, client) -> Scorer | None:
    if not env.get("GEMINI_API_KEY"):
        log.warning("scoring disabled: GEMINI_API_KEY not set (all filtered jobs will be sent)")
        return None
    try:
        resume = open(cfg.profile.resume_path, encoding="utf-8").read()
    except OSError:
        log.warning("scoring disabled: cannot read %s", cfg.profile.resume_path)
        return None
    return Scorer(env["GEMINI_API_KEY"], cfg.scoring.model, resume, client, cfg.scoring.fallback_model)


def load_env_file(path: str = ".env") -> None:
    """Minimal KEY=VALUE loader for local runs; real environment variables win."""
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#") and value.strip():
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def main() -> None:
    ap = argparse.ArgumentParser(description="Job Radar")
    ap.add_argument("--dry-run", action="store_true", help="print alerts, no Telegram, no job DB writes")
    ap.add_argument("--source", help="run one source by name, e.g. ibm, emirates, pagewatch")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--db", default="data/state.db")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its request lines include secrets in URLs

    cfg = load_config(args.config)
    load_env_file()
    env = os.environ
    if not args.dry_run and not (env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID")):
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required (or use --dry-run)")

    client = new_client()
    db = Db(args.db, dry=args.dry_run)
    notifier = Notifier(env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", ""), client, dry=args.dry_run)
    stats = run(cfg, env, db, notifier, client, args.dry_run, args.source, build_scorer(cfg, env, client))
    log.info("run summary: %s", dict(sorted(stats.items())))


if __name__ == "__main__":
    main()
