# Job Radar: design notes

A personal job-alert agent. It finds new postings in the UAE (priority) and India for a fresh CS graduate (0-1 years),
removes duplicates, filters them by rules, scores them against a resume with Gemini, and sends Telegram alerts.
It runs on a GitHub Actions schedule at close to zero cost.

## Pipeline

fetch -> normalize -> exact dedupe -> rule filter -> fuzzy dedupe -> Gemini score -> Telegram -> save to SQLite

- **Sources:** Greenhouse, Lever, Ashby, SmartRecruiters, Emirates and IBM career APIs, JSearch (RapidAPI), a page
  watcher for career pages with no API, and read-only IMAP parsing of job-board alert emails (LinkedIn, Indeed,
  Naukri, Glassdoor).
- **No scraping** of job boards. They are reached only through official APIs, aggregators or the alert emails.
- **Filter:** excluded keywords, country, posting age, required years of experience, and a fresher-title rule for
  email alerts (which carry no description).
- **Scoring:** the resume and the job go to Gemini, which returns a 0-100 score and a short reason. Only scores of 65
  or more are sent. Scores are cached, a run scores at most 60 jobs, and a fallback model is used if the primary is down.
- **State:** SQLite (`data/state.db`) holds seen jobs, scores, processed emails and API usage. In CI it is kept on a
  `state` branch between runs.
- **Quota:** JSearch is capped at 180 requests a month, with a Telegram warning at 150. Every Gemini request is
  counted per model per day, with a hard stop at the free-tier limit and a warning before it.

## Agent features (Phase 3)

- **Posting guard:** job postings are untrusted text that sits next to the resume in the prompt. Hidden HTML and
  invisible characters (zero-width, bidi, Unicode tag characters) are removed before the model sees a posting, text
  aimed at an AI ("ignore previous instructions") is flagged on the alert, and the prompt marks the posting as data.
- **Cover notes:** for strong matches, a draft is checked in code (every name and number must appear in the resume,
  posting or research), reviewed by a second call acting as the hiring manager, revised once, and refused if anything
  is still unbacked. Notes get a lower daily budget than scoring.
- **Telegram commands:** `/cover`, `/cv`, `/why`, `/pause`, `/resume`, `/status`, keyed by a short `#ref` on each
  alert. A Cloudflare Worker relays Telegram's webhook to a GitHub `workflow_dispatch`, so replies take about a
  minute with no server; without it, each scheduled run polls for commands.
- **Tailored CV:** `/cv` rewords and reorders the resume for one job, then a code check (skills and headings may only
  use resume words), a review call and one fix; the result is a one-page PDF. Contact details come from secrets.
- **Evaluation:** `python -m job_radar.eval` samples live postings for hand labelling (apply/skip) and reports the
  scorer's catch rate, noise and best threshold, so the alert threshold is set from data.
- **Least privilege:** the model has no tools (search grounding aside); it only returns text that code validates
  and decides what to send.

## Rules the project follows

- Secrets live in `.env` locally and in GitHub Secrets in CI. The resume is never committed; CI writes it from a secret.
- The tool finds, scores and notifies. It never applies, and never logs in to a job site.
- All HTTP is mocked in tests (`pytest` + `respx`).

See `README.md` for setup.
