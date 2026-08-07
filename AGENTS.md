# AGENTS.md — NHS Job Search

Guidance for AI agents working in this repository.

## Project purpose

Single-file Python scraper (`nhs_job_search.py`) that searches [NHS Jobs](https://www.jobs.nhs.uk) for rotational Band 5 physiotherapist posts, downloads the supporting documents attached to each advert, summarises them with an LLM, and writes a cumulative HTML report.

## Layout

- `nhs_job_search.py` — the entire application (~1000 lines, no package structure).
- `config.ini` — live config (gitignored, contains credentials; **never read or print its secrets**). `config.ini.example` is the safe template.
- `requirements.txt` — `requests`, `beautifulsoup4`, `PyPDF2`, `python-docx`.
- `output/` — generated artifacts (gitignored):
  - `jobs_report_YYYYMMDD.html` — styled report per run date.
  - `jobs_data.json` — cumulative structured data for all unique jobs ever scraped.
  - `seen_references.json` — dedup store: reference → {date, title, trust}; processed jobs are skipped on later runs.
  - `documents/` — downloaded PDF/DOC/DOCX files, named by job reference.
- `.venv/` — project virtual environment; always use `.venv\Scripts\python` to run the script.

## How to run

```bash
.venv\Scripts\python nhs_job_search.py
```

Overrides via environment variables (Windows `set` syntax shown in README): `SEARCH_KEYWORD`, `SEARCH_URL`, `EXCLUDE_TERMS`, `MAX_JOBS` (default 10), `MAX_CANDIDATES` (default 50), `MAX_AGE_DAYS` (default 7), `NHS_OUTPUT_DIR` (relocate config/output).

## Hard requirements and behaviour

- **LLM is mandatory**: `main()` exits if `[LLM]` in `config.ini` is missing provider/API key/model. Supported: `kimi.ai`/`moonshot`, `openai`, or any OpenAI-compatible base URL.
- **Login is optional**: NHS Jobs search is public; `[Login]` credentials only enable candidate-only features.
- **Filtering** (all must hold): advert relates to the configured profession, mentions an included band (e.g. Band 5), mentions required terms (e.g. "rotational"); adverts mentioning excluded higher bands (6, 7+) are rejected. Exclusion terms (e.g. "respiratory") match against the **job title only**, not body text — do not change this without asking, it is deliberate.
- **Date window**: only adverts posted within the last `MAX_AGE_DAYS` days (default 7) are processed. The date is read from the search result's `search-result-publicationDate` field and re-verified against the "Date posted" `h3`/`p` pair on the job page (`parse_posted_date` handles "7 August 2026" and "07/08/2026"). Jobs with unparseable dates are kept, not dropped.
- **Sorting**: `sort=publicationDateDesc` is added to the search URL when absent (works with both `searchFormType=main` and `sortBy`), so results arrive newest-first; the report is sorted by date posted, descending.
- **Politeness**: 1-second delay between requests; `MAX_RETRIES = 3` on HTTP errors. Keep this intact.
- **Cumulative data, windowed report**: never overwrite `jobs_data.json` / `seen_references.json`; always merge. The HTML report shows only jobs posted within the date window (aged-out jobs remain in `jobs_data.json`). Report filenames include the run date (`YYYYMMDD`).
- **Legacy `.doc`** extraction uses Microsoft Word COM (Windows-only); without Word the file is saved for manual review. `.pdf`/`.docx` use PyPDF2 / python-docx.

## Conventions

- Windows + Git Bash environment; Python 3.10+.
- No test suite exists — verify changes by running the scraper with small limits, e.g. `MAX_JOBS=2 MAX_CANDIDATES=5`, and inspecting the generated report.
- Configurable filters live in the `[Filters]` section (`profession`, `bands`, `required_terms`, `exclude_bands`); defaults target Band 5 rotational physiotherapy.
- Keep the single-file structure; resist splitting into modules unless the user asks.

## When you change things

- If you change behaviour, config keys, outputs, or conventions, update **both** `README.md` and this `AGENTS.md`.
