# Job Radar

Finds new UAE (priority) and India jobs and sends them to Telegram. See [PLAN.md](PLAN.md). Phase 1: no Claude scoring, everything that passes the filter is sent.

## 1. Get the keys

| Secret | Where |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram → chat with **@BotFather** → `/newbot` → copy the token |
| `TELEGRAM_CHAT_ID` | Send any message to your new bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `message.chat.id` |
| `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | https://developer.adzuna.com (free). India only |
| `RAPIDAPI_KEY` | https://rapidapi.com → subscribe to **JSearch** (free plan, 200 requests/month hard limit) |
| `GEMINI_API_KEY` | https://aistudio.google.com → Get API key (free tier). Optional: enables scoring |
| `IMAP_USER`, `IMAP_APP_PASSWORD` | A dedicated Gmail: enable 2-Step Verification, then https://myaccount.google.com/apppasswords → create an app password (16 chars). IMAP is on by default in Gmail |

ATS boards (Greenhouse, Lever, Ashby) need no key. Add company tokens in `config.yaml` under `companies`.

## 2. Edit `config.yaml`

Replace the PLACEHOLDER roles, skills and cities. `jsearch.queries` holds at most 3 queries (UAE first).

## 3. Run locally

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in the values; the tool reads .env automatically (it is gitignored)
python -m job_radar.main --dry-run               # prints alerts, no Telegram, no job DB writes
python -m job_radar.main --dry-run --source lever
python -m job_radar.main --dry-run --source jsearch   # forces JSearch; SPENDS real quota
python -m pytest && python -m ruff check .
```

## 4. Deploy on GitHub Actions

1. Create a **private** repo and push this folder. `resume.md` and `.env` are gitignored. If the repo is public, store the resume as a secret instead (needed from Phase 2).
2. Repo → Settings → Secrets and variables → Actions → add the secrets above, plus `RESUME_TEXT` (paste the whole of `resume.md`). Adzuna is paid now (leave it out).
3. Actions tab → **job-radar** → *Run workflow* to test; afterwards it runs hourly (edit the cron in `.github/workflows/run.yml`).

**State:** `data/state.db` is force-pushed as a single commit to a `state` branch after each run (no history growth) and restored at the start of the next. Alternative `actions/cache` can be evicted; the branch can't. Workflow `concurrency` queues overlapping runs.

## Email alerts (Phase 1.5)

Create job alerts on LinkedIn, Indeed UAE, Naukri, Glassdoor, Naukrigulf, Bayt and GulfTalent, all delivered to the dedicated Gmail. Each run reads the last 7 days (max 50 mails), read-only (`BODY.PEEK`, mailbox opened read-only; nothing is deleted, flagged or sent). Processed mails are remembered by Message-ID in the DB, and only after their jobs were delivered. If a mail's layout isn't recognised you still get one alert with the subject and first link.

Parsers for LinkedIn, Indeed, Naukri and Glassdoor are built and tested against real (anonymised) samples in `tests/fixtures/`. Naukrigulf, Bayt and GulfTalent are still guesses (fallback alert only) until you add samples as `tests/fixtures/<sender>.eml` (download from Gmail: ⋮ menu → Download message). Debug locally with `python -m job_radar.main --dry-run --source email`.

## Company portals (Etihad, Emirates, IBM, TCS)

None of these four use Workday, so no Workday source was built. What each uses:

| Company | ATS | How it's covered |
|---|---|---|
| Etihad | SmartRecruiters (`EtihadAirways5`) | Public API, `companies.smartrecruiters` in `config.yaml`. The list has no job description, so the experience filter can't see it |
| Emirates Group | Avature behind a custom site | The JSON feed the site's own search page uses (`portals.emirates`). No per-job link exists, so alerts link to the search page |
| IBM | Custom (`careers.ibm.com`) | The undocumented search API behind ibm.com/careers (`portals.ibm`), UAE + India, **Entry Level and Internship only** (about 99% of IBM's roles there are tagged "Professional"; edit `LEVELS` in `job_radar/sources/ibm.py` to widen). May break without notice |
| TCS | Custom (iBegin) | `tcs.com` answers 403 to non-browser clients, so the page watcher can't read it. It stays in `page_watch` and will trigger the "failed 3 runs" warning; for TCS use Naukri/LinkedIn alerts instead |

The **page watcher** (`page_watch` in `config.yaml`) sends "Page changed: <name>" when a page's text changes (numbers are ignored, scripts stripped). The first run only records a baseline.

## Smart matching (Gemini scoring)

With `GEMINI_API_KEY` set (free key from https://aistudio.google.com → Get API key), every job that passes the filter is scored 0-100 against `resume.md` by `scoring.model` in `config.yaml`. Only scores ≥ `notify_threshold` (65) alert you, with the reason and missing skills; lower ones are saved as `low_score`. Without the key everything that passes the filter is sent, as before.

- `resume.md` is the text sent to Google: it has no name, phone, email or links on purpose. On Gemini's free tier prompts may be used to improve Google products, so keep it that way. It is gitignored; for GitHub Actions add its full text as the secret `RESUME_TEXT`.
- Cache: each job is scored once (table `scores`). Per run at most `max_jobs_scored_per_run` (60) are scored, UAE first; the rest wait for the next run. About 4.5 s pause between calls keeps within the free ~15 requests/minute.
- If scoring fails twice: UAE jobs are sent anyway (marked unscored), India jobs retry next run.
- The run log prints calls and tokens used. A dry run also scores, so it spends free quota.

## JSearch quota

Free plan = 200 requests/month. Every request is counted in SQLite (`api_usage`). The tool stops calling at 180/month and sends one Telegram warning at 150. JSearch runs only when `min_hours_between_runs` (10) have passed since its last run, so about twice a day even though the workflow is hourly, with ≤3 queries per run, `date_posted=3days`.
