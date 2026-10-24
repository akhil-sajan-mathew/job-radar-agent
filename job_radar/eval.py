"""How good is the scorer? Measured against your own apply/skip labels.

  python -m job_radar.eval sample [--n 40]  live postings that pass your filters -> eval/labels.csv
                                            (fill the `label` column with apply or skip) + eval/jobs.jsonl
  python -m job_radar.eval run [--rescore]  scores the labelled jobs (cached in eval/scores.json, so re-running
                                            the report is free) and prints catch rate, noise and the best threshold

eval/ is gitignored: it holds your labels and posting text.
"""
import argparse
import csv
import json
import logging
import os
import random
from pathlib import Path

from .config import Config, load_config
from .db import Db
from .filter import reject_reason
from .main import build_scorer, build_sources, load_env_file
from .models import Job
from .sources.base import new_client

DIR = Path("eval")
APPLY, SKIP = {"apply", "a", "yes", "y", "1"}, {"skip", "s", "no", "n", "0"}


def sample(cfg: Config, env, client, n: int, out: Path = DIR) -> int:
    """Writes up to n postings that pass the rule filter (age ignored, so older jobs count too)."""
    if (out / "labels.csv").exists():
        raise SystemExit(f"{out / 'labels.csv'} exists: it may hold your labels. Move it away to sample again.")
    cfg = cfg.model_copy(deep=True)
    cfg.filters.max_job_age_days = 100000
    env = {k: v for k, v in env.items() if k != "RAPIDAPI_KEY"}  # JSearch has a monthly quota: not for this
    jobs: dict[str, Job] = {}
    for src in build_sources(cfg, env, client, Db(":memory:"), print):
        if src.name == "pagewatch":
            continue
        try:
            jobs.update((j.id, j) for j in src.fetch() if not reject_reason(j, cfg))
        except Exception as e:
            print(f"{src.name} failed: {e}")
    picked = random.Random(0).sample(sorted(jobs.values(), key=lambda j: j.id), min(n, len(jobs)))
    out.mkdir(exist_ok=True)
    (out / "jobs.jsonl").write_text("".join(j.model_dump_json() + "\n" for j in picked), encoding="utf-8")
    with open(out / "labels.csv", "w", newline="", encoding="utf-8-sig") as f:  # -sig: Excel reads UTF-8 right
        w = csv.writer(f)
        w.writerow(["id", "label", "title", "company", "location", "source", "url"])
        w.writerows([j.id, "", j.title, j.company, j.location, j.source, j.url] for j in picked)
    return len(picked)


def load_labels(path: Path) -> dict[str, bool]:
    """{job id: True for apply, False for skip}; blank or unknown labels are left out."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    out = {}
    for r in rows:
        lab = (r.get("label") or "").strip().lower()
        if lab in APPLY | SKIP:
            out[r["id"]] = lab in APPLY
    return out


def metrics(pairs: list[tuple[bool, int]], t: int) -> tuple[float, float, int]:
    """(catch rate: share of your apply jobs scoring >= t, noise: share of alerts you'd skip, alerts)."""
    sent = [apply for apply, score in pairs if score >= t]
    wanted = sum(apply for apply, _ in pairs)
    catch = sum(sent) / wanted if wanted else 0.0
    noise = (len(sent) - sum(sent)) / len(sent) if sent else 0.0
    return catch, noise, len(sent)


def report(pairs: list[tuple[bool, int]], details: list[tuple[bool, int, str, str]], threshold: int) -> str:
    lines = [f"{len(pairs)} labelled jobs: {sum(a for a, _ in pairs)} apply, {sum(not a for a, _ in pairs)} skip", "",
             "threshold  catch rate  noise  alerts"]
    best, best_f1 = threshold, -1.0
    for t in range(30, 95, 5):
        c, nz, k = metrics(pairs, t)
        f1 = 2 * c * (1 - nz) / (c + 1 - nz) if c + 1 - nz else 0.0
        if f1 > best_f1:
            best, best_f1 = t, f1
        lines.append(f"{t:>9}  {c:>10.0%}  {nz:>5.0%}  {k:>6}{'   <- current' if t == threshold else ''}")
    c, nz, _ = metrics(pairs, threshold)
    lines += ["", f"Current threshold {threshold}: catches {c:.0%} of the jobs you'd apply to; {nz:.0%} of alerts "
              f"are ones you'd skip.", f"Best balance (F1) at threshold {best}.", "",
              "You'd apply, scored lowest:"]
    lines += [f"  {s:>3}  {title}: {why}" for a, s, title, why in sorted(details, key=lambda d: d[1]) if a][:5]
    lines += ["You'd skip, scored highest:"]
    lines += [f"  {s:>3}  {title}: {why}" for a, s, title, why in sorted(details, key=lambda d: -d[1]) if not a][:5]
    return "\n".join(lines)


def run(cfg: Config, scorer, rescore: bool = False, out: Path = DIR) -> str:
    labels = load_labels(out / "labels.csv")
    if not labels:
        raise SystemExit(f"No apply/skip labels in {out / 'labels.csv'} yet.")
    jobs = {j.id: j for j in (Job.model_validate_json(x) for x in (out / "jobs.jsonl").read_text("utf-8").splitlines())}
    cache_path = out / "scores.json"
    cache = {} if rescore or not cache_path.exists() else json.loads(cache_path.read_text("utf-8"))
    for jid in labels:
        if jid not in cache and jid in jobs:
            m = scorer.score(jobs[jid])
            if m:
                cache[jid] = {"score": m.score, "reason": m.reason}
                cache_path.write_text(json.dumps(cache, indent=1), encoding="utf-8")  # keep progress if stopped
    done = [jid for jid in labels if jid in cache]
    details = [(labels[j], cache[j]["score"], jobs[j].title, cache[j]["reason"]) for j in done]
    missing = len(labels) - len(done)
    text = report([(a, s) for a, s, _, _ in details], details, cfg.scoring.notify_threshold)
    return text + (f"\n\n{missing} labelled jobs couldn't be scored (Gemini busy?): run again." if missing else "")


def main() -> None:
    ap = argparse.ArgumentParser(description="Evaluate the scorer against your labels")
    ap.add_argument("action", choices=["sample", "run"])
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--rescore", action="store_true", help="ignore cached scores (after a prompt/model change)")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    cfg, client = load_config(args.config), new_client()
    load_env_file()
    if args.action == "sample":
        n = sample(cfg, os.environ, client, args.n)
        print(f"Wrote {n} postings to {DIR / 'labels.csv'}. Fill the label column with apply or skip, then run:"
              f"\n  python -m job_radar.eval run")
        return
    scorer = build_scorer(cfg, os.environ, client, Db("data/state.db"), print)
    if not scorer:
        raise SystemExit("Needs GEMINI_API_KEY and resume.md")
    print(run(cfg, scorer, args.rescore))


if __name__ == "__main__":
    main()
