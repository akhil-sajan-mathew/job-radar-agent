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
- **Quota:** JSearch is capped at 180 requests a month, with a Telegram warning at 150.

## Rules the project follows

- Secrets live in `.env` locally and in GitHub Secrets in CI. The resume is never committed; CI writes it from a secret.
- The tool finds, scores and notifies. It never applies, and never logs in to a job site.
- All HTTP is mocked in tests (`pytest` + `respx`).

See `README.md` for setup.
