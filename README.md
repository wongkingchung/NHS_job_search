# NHS Rotational Band 5 Physiotherapist Job Search

A small Python scraper that searches [NHS Jobs](https://www.jobs.nhs.uk) for rotational Band 5 physiotherapist posts, downloads the supporting documents attached to each advert and writes a Markdown report summarising the key points.

## What it does

1. Reads `config.ini` for optional login credentials and search settings.
2. Searches NHS Jobs using a configurable search URL (e.g., your filtered NHS Jobs link).
3. Keeps only physiotherapy-related adverts and verifies each detail page mentions **Band 5** and **rotational**.
4. Applies exclusion terms from config (e.g., skip jobs with "respiratory" in the title).
5. Downloads supporting documents (PDF/DOC/DOCX) attached to each advert.
6. Extracts text from the documents and the advert page.
7. Visits each employer's listed website and summarises key trust information.
8. Tracks processed job references so duplicates are skipped in future runs.
9. Writes (report file includes the run date as `YYYYMMDD`):
   - `output/jobs_report_YYYYMMDD.html` — styled HTML report; open in any browser.
   - `output/jobs_data.json` — cumulative structured data for all unique jobs.
   - `output/seen_references.json` — list of references already processed.
   - `output/documents/` — downloaded supporting documents.


## Requirements

- Python 3.10+
- Windows (the legacy `.doc` extractor uses Microsoft Word COM if available)
- Dependencies listed in `requirements.txt`

Create and use a project virtual environment (recommended):

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

Then run the scraper with the venv interpreter:

```bash
.venv\Scripts\python nhs_job_search.py
```

## Configuration

Create or edit `config.ini` in the project root:

```ini
[Login]
url=https://www.jobs.nhs.uk/candidate/search
email=your.email@example.com
pwd=yourpassword

[Search]
url=https://www.jobs.nhs.uk/candidate/search/results?keyword=physiotherapist&payBand=BAND_5&skipPhraseSuggester=true&searchFormType=sortBy&sort=publicationDateDesc&language=en
keyword=physiotherapist
exclude=respiratory
```

> **Note:** Login is optional. NHS Jobs search is public, so the scraper works without valid credentials. The login section is only there in case you want to access candidate-only features later.
>
> `url` under `[Search]` lets you paste a pre-filtered NHS Jobs results URL. The scraper preserves its query parameters and pages through the results. If you leave it blank, it builds a default search from `keyword`.

## Usage

Run with defaults (up to 10 matching jobs from 50 candidates):

```bash
python nhs_job_search.py
```

Control the number of results via environment variables:

```bash
set MAX_JOBS=20
set MAX_CANDIDATES=100
python nhs_job_search.py
```

Or override settings for one run:

```bash
set SEARCH_KEYWORD=band 5 physiotherapist
set SEARCH_URL=https://www.jobs.nhs.uk/candidate/search/results?keyword=physiotherapist&payBand=BAND_5&language=en
set EXCLUDE_TERMS=respiratory, paediatric
python nhs_job_search.py
```

## Output

- `output/jobs_report_YYYYMMDD.html` — the main report; double-click to open in your browser.
- `output/jobs_data.json` — cumulative raw scraped data for all unique jobs seen so far.
- `output/seen_references.json` — references already processed, with date scraped, job title, and trust name; used to skip duplicates.
- `output/documents/` — downloaded files named by job reference.

## Important notes

- The scraper adds a 1-second delay between requests to be polite to the NHS Jobs and trust websites.
- Legacy `.doc` files are extracted using Microsoft Word COM on Windows. If Word is not installed, those files are saved for manual review.
- The filters applied are: title/page text must relate to physiotherapy, mention **Band 5**, and mention **rotational**. Adverts that mention higher bands (e.g., Band 6, 7) are excluded.
- Exclusion terms are matched against the **job title** so that general rotational posts which mention an excluded specialty in the body text are not removed.
- Trust website summaries are best-effort extracts from the employer's homepage.
- Reports and `jobs_data.json` are cumulative: each run adds only new, unique jobs and keeps all previously seen ones.
