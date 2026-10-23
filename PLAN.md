# Job Radar: Build Plan

A personal job-alert agent that finds new postings matching my resume in the **UAE (preferred) and India**, scores them against my resume with Claude, and sends instant Telegram alerts. Runs on a schedule via GitHub Actions at zero or near-zero cost.

This file is the source of truth for the project. Read it fully before writing code, build phase by phase, and stop for my review at the end of each phase.

---

## 1. Goals and non-goals

**Goals**
- See relevant new jobs within about an hour of posting, so I can be among the first applicants.
- Cover many sources: aggregator APIs, company ATS job boards, parsed email alerts from job boards, and graduate-program pages.
- Only notify me about realistic matches for a fresh CS engineering graduate (0–1 years of experience).
- UAE jobs prioritized over India jobs in ranking and in notifications.
- Cheap, simple, maintainable by one person.

**Non-goals (hard rules)**
- **Do not scrape LinkedIn, Indeed, Glassdoor, Naukri, Bayt, GulfTalent or Naukrigulf directly.** Their terms prohibit it and it risks account bans. Get their listings only through official APIs, Google Jobs aggregators, or by parsing the alert emails they send me.
- Never log into any job site with my credentials from code.
- No auto-applying. The tool finds, scores and notifies; I apply manually.
- No headless-browser scraping in Phase 1–2. Plain HTTP + JSON only.

---

## 2. Tech stack

- Python 3.11+
- `httpx` (HTTP, with timeouts and retries), `pydantic` (models and config validation), `PyYAML` (config), `sqlite3` (state), `anthropic` (scoring), `rapidfuzz` (fuzzy dedupe), `pytest` + `respx` (tests with mocked HTTP)
- Telegram Bot API via plain HTTP calls (no heavy library needed)
- GitHub Actions for scheduling
- `ruff` for linting, type hints throughout

Before using any external API, **check its current documentation** (endpoints, parameters, rate limits, free-tier limits). The endpoints listed below are a starting point and may have changed.

---

## 3. Repository structure

```
job-radar/
├── PLAN.md                  # this file
├── README.md                # setup instructions written for me
├── config.yaml              # my preferences (roles, skills, locations, companies)
├── resume.md                # my resume in plain text/markdown (gitignored if repo is public)
├── .env.example             # names of required secrets, no values
├── requirements.txt
├── job_radar/
│   ├── __init__.py
│   ├── main.py              # entry point: fetch → normalize → dedupe → filter → score → notify
│   ├── config.py            # loads and validates config.yaml with pydantic
│   ├── models.py            # Job model (see §5)
│   ├── db.py                # SQLite state: seen jobs, scores, notification status
│   ├── sources/
│   │   ├── base.py          # Source interface
│   │   ├── greenhouse.py
│   │   ├── lever.py
│   │   ├── ashby.py
│   │   ├── adzuna.py        # India only (Adzuna does not cover UAE)
│   │   ├── google_jobs.py   # JSearch and/or SerpAPI; main UAE source
│   │   ├── email_alerts.py  # Phase 2: parse LinkedIn/Naukri/Bayt/etc. alert emails via IMAP
│   │   ├── page_watch.py    # Phase 3: detect changes on graduate-program pages
│   │   └── workday.py       # Phase 3: optional, fragile
│   ├── filter.py            # rule-based pre-filter
│   ├── scorer.py            # Claude-based resume matching
│   └── notify.py            # Telegram messages
├── data/
│   └── state.db             # SQLite state (persisted between runs, see §9)
├── tests/
└── .github/workflows/run.yml
```

---

## 4. Configuration (`config.yaml`)

Create this with placeholder values and ask me to fill in roles and skills before the first real run.

```yaml
profile:
  target_roles: ["software engineer", "backend developer", "graduate engineer"]   # PLACEHOLDER
  skills: ["python", "java", "sql", "react"]                                      # PLACEHOLDER
  experience_max_years: 1
  resume_path: resume.md

locations:
  uae:
    priority: 1
    cities: ["Dubai", "Abu Dhabi", "Sharjah", "United Arab Emirates"]
  india:
    priority: 2
    cities: ["Bangalore", "Hyderabad", "Pune", "Chennai", "Remote India"]         # PLACEHOLDER

filters:
  include_keywords: ["engineer", "developer", "graduate", "trainee", "associate", "junior", "fresher", "intern"]
  exclude_keywords: ["senior", "sr.", "lead", "principal", "staff", "manager", "architect", "head of"]
  max_job_age_days: 7

scoring:
  model: "claude-haiku-5-5"      # cheap model for scoring; configurable
  notify_threshold: 65           # 0-100
  max_jobs_scored_per_run: 60    # cost guardrail

companies:                       # ATS board tokens; add more over time
  greenhouse: []                 # e.g. board tokens found in boards.greenhouse.io/<token>
  lever: []                      # e.g. jobs.lever.co/<company>
  ashby: []                      # e.g. jobs.ashbyhq.com/<company>

google_jobs:
  provider: "jsearch"            # "jsearch" or "serpapi"
  queries_per_run: 6             # keep within free-tier quota
```

Validate the config at startup and fail with a clear error message if something is missing or malformed.

---

## 5. Data model

```python
class Job(BaseModel):
    id: str                 # stable hash of (source, source_job_id) or of (company, title, location) if no id
    source: str             # "greenhouse", "adzuna", "jsearch", "email:linkedin", ...
    title: str
    company: str
    location: str
    country: Literal["AE", "IN", "OTHER"]
    url: str                # apply/listing link
    description: str        # plain text, HTML stripped, truncated to ~6000 chars
    posted_at: datetime | None
    fetched_at: datetime
    salary: str | None
    remote: bool | None
```

Write a helper that maps raw location strings to `country` (handle "Dubai - United Arab Emirates", "Bengaluru", "UAE", "Remote", etc.).

---

## 6. Sources

Every source implements:

```python
class Source(Protocol):
    name: str
    def fetch(self) -> list[Job]: ...
```

Each source must be isolated: if one fails (timeout, 429, changed format), log it, skip it, and continue with the rest. Use 15-second timeouts, polite request pacing, and a descriptive User-Agent.

**Phase 1 sources**

| Source | Covers | Endpoint (verify in current docs) | Notes |
|---|---|---|---|
| Greenhouse | Company boards | `GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | Public, no key |
| Lever | Company boards | `GET https://api.lever.co/v0/postings/{company}?mode=json` | Public, no key |
| Ashby | Company boards | `GET https://api.ashbyhq.com/posting-api/job-board/{name}` | Public, no key |
| Adzuna | India | `GET https://api.adzuna.com/v1/api/jobs/in/search/{page}` with `app_id`, `app_key`, `what`, `where`, `max_days_old` | Free key; does not cover UAE |
| JSearch (RapidAPI) or SerpAPI Google Jobs | UAE + India | JSearch `/search` with `query`, `country`, `date_posted`; or SerpAPI `engine=google_jobs` with `location` | Main UAE source; watch free-tier quotas, rotate queries across runs |

For Google Jobs queries, build them from `target_roles × UAE cities` first, then India. Rotate through the combinations across runs so the free quota lasts the month, prioritizing UAE.

**Phase 2: email alerts**

I will set up job alerts on LinkedIn, Indeed UAE, Naukri, Naukrigulf, Bayt and GulfTalent, delivered to a dedicated Gmail address.
- Read them via IMAP using a Gmail app password (`IMAP_USER`, `IMAP_APP_PASSWORD` secrets). Read-only; never delete or send email.
- One parser per sender, extracting title, company, location and link from the email HTML. Parsers must be defensive: if parsing fails, fall back to sending me the email subject and the first link rather than crashing.
- Mark processed emails by Message-ID in the database, not by modifying the mailbox.
- Save one sample email per sender (with personal info removed) in `tests/fixtures/` for parser tests. Ask me to provide these.

**Phase 3: optional sources**
- `page_watch.py`: a list of graduate-program / early-careers page URLs in config. Store a hash of the relevant page text; if it changes, notify me with the URL ("page changed, check it"). Useful for UAE graduate programs with application windows.
- `workday.py`: some large UAE employers use Workday, whose career sites call a JSON endpoint (`/wday/cxs/{tenant}/{site}/jobs`). Implement only if I ask for it, mark it as fragile, and make failures silent except in logs.

---

## 7. Pipeline (`main.py`)

1. Load config and secrets.
2. Fetch from all enabled sources (concurrently is fine, but respect per-source pacing).
3. Normalize into `Job` objects; strip HTML; detect country.
4. **Dedupe**:
   - Exact: skip any `id` already in the database.
   - Fuzzy: the same job often appears on several sources. Treat as duplicate if company matches (normalized) and title similarity ≥ 90 (rapidfuzz) and country matches. Keep the version with the most direct apply link (company site / ATS over aggregator).
5. **Rule-based filter** (cheap, before any API spend):
   - Drop if title contains an exclude keyword.
   - Drop if posted more than `max_job_age_days` ago.
   - Drop if country is not AE or IN.
   - Drop if the description clearly requires more experience than allowed (regex for patterns like "3+ years", "minimum 4 years", "5-7 years"). Be careful not to drop "0-2 years" or "1+ years".
   - Keep if title or description matches any include keyword or target role.
   - Make the filter lenient rather than strict. UAE fresher postings are scarce, and missing a good job is worse than scoring an extra one.
6. **Score with Claude** (§8), up to `max_jobs_scored_per_run`, UAE jobs first, newest first.
7. **Notify** jobs scoring ≥ threshold (§10).
8. Save everything (including dropped and low-scoring jobs, with the reason) to the database so nothing is re-processed.
9. Print a run summary: per-source counts, filtered, scored, notified, errors.

Add a `--dry-run` flag (no Telegram messages, no DB writes, prints what would be sent) and a `--source <name>` flag to run one source for debugging.

---

## 8. Scoring with Claude (`scorer.py`)

- Send the resume and the job (title, company, location, description) to the model in `config.scoring.model`.
- Require JSON-only output, validated with pydantic:

```json
{
  "score": 0-100,
  "reason": "one short sentence on why it fits or doesn't",
  "experience_required": "e.g. '0-2 years' or 'not stated'",
  "fresher_friendly": true,
  "visa_or_relocation": "sponsors | local candidates only | not stated",
  "missing_skills": ["..."]
}
```

- Scoring rubric in the prompt: role/skill match to the resume, whether experience requirements fit a fresh graduate, and for UAE roles whether non-local candidates appear eligible. Penalize roles needing 3+ years heavily. Do not penalize "not stated" experience.
- Cache scores by job `id` so a job is never scored twice.
- On API errors or invalid JSON: retry once, then store the job as `score_failed` and notify it anyway if it passed the rule filter and is in the UAE (better a false positive than a missed job).
- Log token usage per run so I can track cost.

---

## 9. State and scheduling

- GitHub Actions workflow running on a cron schedule (every 60 minutes to start; make it easy to change) plus `workflow_dispatch` for manual runs.
- Persist `data/state.db` between runs. Preferred approach: commit it to a dedicated `state` branch at the end of each run. Alternative: `actions/cache` with a fallback. Choose one, explain the trade-off briefly in the README, and make sure two overlapping runs can't corrupt it (use workflow `concurrency`).
- Secrets via GitHub Actions secrets: `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, `RAPIDAPI_KEY` or `SERPAPI_KEY`, and in Phase 2 `IMAP_USER`, `IMAP_APP_PASSWORD`.
- If the repo is public, the resume must not be committed. Store it as a secret (`RESUME_TEXT`) and write it to a temp file at runtime, or recommend a private repo. Ask me which I prefer.
- The DB contains job data only, no secrets or personal info.

---

## 10. Notifications (`notify.py`)

- One Telegram message per matching job, UAE jobs sent first, then India, each sorted by score.
- Format:

```
🇦🇪 82/100 · Graduate Software Engineer
Company X · Dubai
Posted 2h ago · via Greenhouse
✅ Fresher-friendly · Visa: not stated
Why: Strong Python/SQL match; 0-1 yrs required.
Missing: Docker
Apply: <link>
```

- If more than 10 matches in one run, send the top 10 individually and one summary message with the rest as a short list.
- If a source has failed 3 runs in a row, send me one warning message (not every run).
- Respect Telegram rate limits (small delay between messages).
- README must include step-by-step instructions to create the bot with BotFather and find my chat ID.

---

## 11. Testing and quality

- Unit tests for: location → country mapping, experience regex (include tricky cases like "0-2 years", "2+ years preferred", "minimum 3 years"), dedupe logic, each source's parser using saved JSON fixtures, scorer JSON validation, and notification formatting.
- Mock all HTTP in tests (`respx`); tests must never hit real APIs or spend money.
- `ruff` clean, type hints on public functions.
- Clear logging (INFO for run summary, DEBUG for details), no secrets in logs.

---

## 12. Build phases and acceptance criteria

Stop after each phase, show me what was built and how to run it, and wait for my go-ahead.

**Decisions (supersede anything below):** JSearch (RapidAPI free plan, 200 req/month hard limit) is the Google Jobs provider; SerpAPI dropped. A persistent monthly counter in SQLite stops JSearch at 180 and warns on Telegram at 150. JSearch runs about twice a day (not hourly) with at most 3 configured queries, UAE first, `date_posted=3days`; all other sources run hourly. The IMAP email-alert parser moves from Phase 2 to **Phase 1.5**, right after the core pipeline works. Repo is private. Scoring uses Google Gemini (free tier, `gemini-3.1-flash-lite`) over plain HTTP instead of Claude/`anthropic`; Adzuna is dropped (paid since the plan was written); JSearch moved to API v5 (`/search-v2`).

**Phase 1: core pipeline (target: running the same day)**
- Repo scaffold, config validation, Job model, SQLite state.
- Sources: Greenhouse, Lever, Ashby, Adzuna (India), JSearch or SerpAPI (UAE + India).
- Rule-based filter, dedupe, Telegram notifications. **No Claude scoring yet**: notify everything that passes the filter.
- `--dry-run` works locally.
- GitHub Actions workflow running hourly with persisted state.
- Done when: I receive real Telegram alerts for new UAE/India jobs and no duplicates across runs.

**Phase 1.5: email alerts**
- IMAP parsing for LinkedIn, Indeed, Naukri, Naukrigulf, Bayt, GulfTalent (details in §6).

**Phase 2: smart matching**
- Claude scoring with threshold, caching and cost logging.
- - Fuzzy cross-source dedupe.
- Done when: alerts include scores and reasons, low-quality matches are suppressed, and email-alert jobs appear in notifications.

**Phase 3: extras (only the ones I ask for)**
- Graduate-program page watcher.
- Workday source for specific companies.
- Daily digest message (summary of yesterday: counts, top jobs I may have missed).
- For a chosen job, generate tailored resume bullet suggestions and a short cover note as a Telegram reply or a file.
- A simple `/stats` or `/pause` Telegram command (only if it stays simple).

---

## 13. Working agreement with Claude Code

- Ask me before: adding new dependencies beyond §2, choosing between JSearch and SerpAPI, deciding public vs. private repo handling for the resume, or adding any source not listed here.
- Never hard-code secrets. Never commit `.env` or the resume.
- Prefer small, readable modules over clever abstractions. This is a one-person project.
- When an external API's real behavior differs from this plan, follow the real API and tell me what changed.
- Keep README.md up to date with setup steps a fresh graduate can follow: getting each API key, creating the Telegram bot, adding GitHub secrets, running locally, and triggering the workflow manually.
