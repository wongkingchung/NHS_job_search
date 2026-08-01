#!/usr/bin/env python3
"""
NHS Jobs scraper.

Searches NHS Jobs for "rotational band 5 physiotherapist", scrapes job
listings, downloads supporting documents from each advert and writes a
Markdown report summarising the key points.

Configuration (credentials are only required if you want to log in):
    [Login]
    url=https://www.jobs.nhs.uk/candidate/search
    email=<your email>
    pwd=<your password>
"""

import configparser
import datetime
import hashlib
import io
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlencode, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.ini"
OUTPUT_DIR = BASE_DIR / "output"
DOCS_DIR = OUTPUT_DIR / "documents"
JSON_PATH = OUTPUT_DIR / "jobs_data.json"
SEEN_REFS_PATH = OUTPUT_DIR / "seen_references.json"

BASE_URL = "https://www.jobs.nhs.uk"
SEARCH_PATH = "/candidate/search/results"
LOGIN_PATH = "/candidate/auth/login"

REQUEST_DELAY = 1.0  # seconds between requests to be polite
MAX_RETRIES = 3

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Keywords used to highlight the most relevant sentences when summarising.
KEY_PHRASES = [
    "essential", "desirable", "requirement", "qualification", "degree",
    "registered", "hcpc", "csp", "experience", "skill", "knowledge",
    "ability", "responsibility", "duty", "main duties", "job summary",
    "person specification", "band 5", "rotational", "physiotherapist",
    "assessment", "treatment", "rehabilitation", "multidisciplinary",
    "communication", "team", "patient", "clinical", "caseload",
    "mentorship", "supervision", "appraisal", "training", "development",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def clean_text(text: str) -> str:
    """Normalise whitespace in a string."""
    return re.sub(r"\s+", " ", text).strip()


def score_sentence(sentence: str) -> int:
    """Score a sentence by how many key job-related words it contains."""
    lowered = sentence.lower()
    return sum(1 for phrase in KEY_PHRASES if phrase in lowered)


def summarise_text(text: str, max_sentences: int = 12) -> str:
    """
    Simple extractive summary.

    Splits the text into sentences, scores each sentence by the presence of
    job-relevant keywords, and returns the top scoring sentences in their
    original order.
    """
    if not text:
        return "No text available to summarise."

    # Split on sentence endings while keeping the delimiters.
    raw_sentences = re.split(r"(?<=[.!?])\s+", text.replace("\n", " "))
    sentences = [clean_text(s) for s in raw_sentences if len(s.split()) > 4]

    if not sentences:
        return clean_text(text)[:1000]

    scored = [(i, score_sentence(s), s) for i, s in enumerate(sentences)]
    scored.sort(key=lambda x: x[1], reverse=True)
    top = scored[:max_sentences]
    top.sort(key=lambda x: x[0])  # restore original order
    return "\n".join(f"- {s}" for _, _, s in top)


def is_rotational(text: str) -> bool:
    """Return True if the text suggests this is a rotational post."""
    return bool(text) and "rotational" in text.lower()


def parse_exclude_terms(terms: str) -> list:
    """Parse a comma-separated exclusion list into normalised tokens."""
    if not terms:
        return []
    return [t.strip().lower() for t in terms.split(",") if t.strip()]


def contains_excluded_term(text: str, exclude_terms: list) -> bool:
    """Return True if text contains any of the excluded terms."""
    if not text or not exclude_terms:
        return False
    lowered = text.lower()
    return any(term in lowered for term in exclude_terms)


# ---------------------------------------------------------------------------
# LLM summarisation
# ---------------------------------------------------------------------------
LLM_PROMPTS = {
    "advert": (
        "You are summarising an NHS job advert for a rotational Band 5 physiotherapist post. "
        "Extract the key points relevant to an applicant: main duties, essential requirements, "
        "desirable criteria, qualifications, professional registration, salary/band, working pattern, "
        "location, and any special mentions. Return concise bullet points, one per line, "
        "starting with '- '."
    ),
    "document": (
        "You are summarising a supporting document for an NHS job advert. "
        "Extract key information relevant to a Band 5 physiotherapist applicant: job summary, "
        "main duties, person specification, essential and desirable criteria, qualifications, "
        "skills, experience, and working conditions. Return concise bullet points, one per line, "
        "starting with '- '."
    ),
    "trust": (
        "You are reading the About Us / Values / Mission page of an NHS trust website. "
        "Summarise the trust's stated values, mission, vision, culture, and what they emphasise "
        "about patient care and staff. Return concise bullet points, one per line, starting with '- '."
    ),
}


class LLMSummarizer:
    """Simple OpenAI-compatible chat-completion summariser."""

    def __init__(self, provider: str, api_key: str, model: str):
        self.provider = (provider or "").lower().strip()
        self.api_key = api_key
        self.model = model
        self.base_url = self._base_url()

    def _base_url(self) -> str:
        if self.provider in ("kimi", "kimi.ai", "moonshot"):
            return "https://api.moonshot.ai/v1"
        if self.provider in ("openai",):
            return "https://api.openai.com/v1"
        # Allow a custom base URL to be passed as the provider if it looks like one.
        if self.provider.startswith(("http://", "https://")):
            return self.provider.rstrip("/")
        return ""

    def is_configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def summarize(self, text: str, prompt_key: str, max_tokens: int = 1200) -> str:
        if not self.is_configured() or not text:
            return ""
        prompt = LLM_PROMPTS.get(prompt_key, LLM_PROMPTS["document"])
        truncated = self._truncate_text(text)
        messages = [
            {"role": "system", "content": "You are a helpful assistant that summarises text accurately."},
            {"role": "user", "content": f"{prompt}\n\n---\n\n{truncated}"},
        ]
        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": 0.2,
                    "max_tokens": max_tokens,
                },
                timeout=60,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            return content.strip()
        except Exception as exc:
            print(f"LLM summarisation failed ({self.provider}): {exc}", file=sys.stderr)
            return ""

    def _truncate_text(self, text: str, max_chars: int = 6000) -> str:
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n\n[Content truncated due to length limits.]"


def load_seen_references(path: Path) -> dict:
    """
    Load previously processed job references as a dict.

    Returns {reference: metadata_dict}. Supports the old flat-list format for
    backward compatibility.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            result = {}
            for item in data:
                if isinstance(item, dict) and item.get("reference"):
                    result[item["reference"]] = item
                elif isinstance(item, str):
                    # Backward compatibility: old format stored just references.
                    result[item] = {
                        "reference": item,
                        "date_scraped": "",
                        "title": "",
                        "trust": "",
                    }
            return result
    except Exception:
        pass
    return {}


def save_seen_references(path: Path, references: dict) -> None:
    """Persist processed job references with metadata, sorted by reference."""
    entries = sorted(references.values(), key=lambda x: x.get("reference", ""))
    path.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")


def load_existing_jobs(path: Path) -> list:
    """Load previously saved job data so reports can be cumulative."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def is_physiotherapy(text: str) -> bool:
    """Return True if the text relates to physiotherapy."""
    if not text:
        return False
    lowered = text.lower()
    return "physiotherapist" in lowered or "physiotherapy" in lowered or "physio" in lowered


def is_band_5(text: str) -> bool:
    """Return True if the text clearly indicates a Band 5 post."""
    if not text:
        return False
    lowered = text.lower()
    return "band 5" in lowered or "band5" in lowered


def is_unwanted_band(text: str) -> bool:
    """Return True if the text mentions a higher band that would exclude Band 5."""
    if not text:
        return False
    lowered = text.lower()
    return any(f"band {b}" in lowered for b in (6, 7, 8, 9)) or "band 6" in lowered


def parse_document_filename(value: str) -> str:
    """
    NHS document button values look like:
        '229-IC-7485068 JDPS.doc (DOC, 668 KB)'
        'Information for Sponsorship (PDF, 206 KB)'
    Return a sensible filename with extension, e.g. '229-IC-7485068 JDPS.doc'
    or 'Information for Sponsorship.pdf'.
    """
    value = clean_text(value)

    # Capture the document type from any '(TYPE, size)' metadata before removing it.
    type_match = re.search(r"\((PDF|DOC|DOCX)", value, re.IGNORECASE)
    inferred_ext = type_match.group(1).lower() if type_match else None

    # Trim metadata parentheses from the end.
    value = re.sub(r"\s*\([^)]*\)\s*$", "", value).strip()

    # If an extension is already present, use it.
    if re.search(r"\.(docx?|pdf)$", value, re.IGNORECASE):
        return value

    ext = inferred_ext or "bin"
    return f"{value}.{ext}"


def extract_sections(text: str) -> dict:
    """
    Try to split a document into common NHS job-description sections.
    Returns a dict of section heading -> paragraph text.
    """
    section_names = [
        "job summary", "main duties", "responsibilities", "person specification",
        "education", "qualifications", "skills", "knowledge", "abilities",
        "experience", "disclosure", "registration",
    ]

    # Heuristic: look for lines that look like section headings.
    pattern = re.compile(
        r"(?:\n|\r|^)\s*(" + "|".join(re.escape(s) for s in section_names) + r")",
        re.IGNORECASE,
    )
    parts = pattern.split(text)
    sections = {}
    current_heading = "Overview"
    for part in parts:
        if part is None:
            continue
        if re.match(
            r"(?:" + "|".join(re.escape(s) for s in section_names) + r")",
            part,
            re.IGNORECASE,
        ):
            current_heading = part.strip().title()
            sections.setdefault(current_heading, [])
        else:
            sections.setdefault(current_heading, []).append(part.strip())

    return {k: clean_text("\n".join(v)) for k, v in sections.items() if v}


# ---------------------------------------------------------------------------
# NHS Jobs client
# ---------------------------------------------------------------------------
class NHSJobsClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
        })

    def _get(self, url: str, timeout: int = 30, **kwargs) -> requests.Response:
        for attempt in range(MAX_RETRIES):
            try:
                resp = self.session.get(url, timeout=timeout, **kwargs)
                resp.raise_for_status()
                time.sleep(REQUEST_DELAY)
                return resp
            except requests.RequestException as exc:
                if attempt == MAX_RETRIES - 1:
                    raise
                time.sleep(2 ** attempt)

    def _post(self, url: str, data: dict, timeout: int = 30, **kwargs) -> requests.Response:
        for attempt in range(MAX_RETRIES):
            try:
                resp = self.session.post(url, data=data, timeout=timeout, **kwargs)
                resp.raise_for_status()
                time.sleep(REQUEST_DELAY)
                return resp
            except requests.RequestException as exc:
                if attempt == MAX_RETRIES - 1:
                    raise
                time.sleep(2 ** attempt)

    def login(self, email: str, password: str) -> bool:
        """
        Optional login. NHS Jobs search is public, but this is provided if
        you want to access candidate-only features later.
        """
        login_url = urljoin(BASE_URL, LOGIN_PATH)
        # Fetch the login page to obtain CSRF token and cookies.
        resp = self._get(login_url)
        soup = BeautifulSoup(resp.text, "html.parser")
        csrf = soup.find("input", {"name": "_csrf"})
        csrf_token = csrf["value"] if csrf else None

        if not csrf_token:
            print("Warning: could not find login CSRF token.", file=sys.stderr)
            return False

        payload = {
            "_csrf": csrf_token,
            "email": email,
            "password": password,
        }
        resp = self._post(login_url, data=payload, allow_redirects=True)
        # A successful login redirects away from the login page.
        return LOGIN_PATH not in resp.url

    def search_jobs(
        self,
        keyword: str,
        search_url: str = "",
        max_candidates: int = 50,
        max_pages: int = 20,
    ) -> list:
        """
        Search NHS Jobs and return candidate job dictionaries.

        If `search_url` is supplied it is used as the base query URL and its
        query parameters are preserved while paging. Otherwise a default URL
        is built from `keyword` plus Band 5 / Allied Health Professionals
        filters. The caller is expected to fetch details and apply stricter
        text filters.
        """
        candidates = []
        page = 1

        # Parse the supplied search URL, or build a default one.
        if search_url:
            parsed = urlparse(search_url)
            base_path = parsed.path or SEARCH_PATH
            base_params = parse_qs(parsed.query, keep_blank_values=True)
            # parse_qs returns lists; flatten single-value params.
            base_params = {k: v[0] if len(v) == 1 else v for k, v in base_params.items()}
        else:
            base_path = SEARCH_PATH
            base_params = {
                "keyword": keyword,
                "language": "en",
                "searchFormType": "main",
                "payBand": "BAND_5",
                "staffGroup": "ALLIED_HEALTH_PROF",
            }

        while len(candidates) < max_candidates and page <= max_pages:
            params = dict(base_params)
            params["page"] = page
            url = urljoin(BASE_URL, base_path) + "?" + urlencode(params)
            print(f"Fetching search page {page}...")
            resp = self._get(url)
            soup = BeautifulSoup(resp.text, "html.parser")
            results = soup.find("ul", class_="search-results")
            if not results:
                break

            items = results.find_all("li", class_="search-result")
            if not items:
                break

            for item in items:
                if len(candidates) >= max_candidates:
                    break
                job = self._parse_search_result(item)
                if not job:
                    continue
                # The NHS keyword search matches many therapist roles; keep
                # only physiotherapy-related Band 5 titles as candidates.
                title = job.get("title", "")
                if is_physiotherapy(title) and not is_unwanted_band(title):
                    candidates.append(job)

            # Stop if there is no "next" page link.
            next_link = soup.find("a", class_="nhsuk-pagination__link--next")
            if not next_link or "disabled" in next_link.get("class", []):
                break
            page += 1

        return candidates

    def _parse_search_result(self, item: BeautifulSoup) -> Optional[dict]:
        title_link = item.find("a", attrs={"data-test": "search-result-job-title"})
        if not title_link:
            return None

        href = title_link.get("href", "")
        relative = href.split("?")[0] if href else ""
        reference = relative.rsplit("/", 1)[-1] if "/" in relative else ""

        employer_block = item.find("div", attrs={"data-test": "search-result-location"})
        employer = ""
        location = ""
        if employer_block:
            h3 = employer_block.find("h3")
            if h3:
                loc_div = h3.find("div", class_="location-font-size")
                if loc_div:
                    location = clean_text(loc_div.get_text(" ", strip=True))
                    # Text before the location div is the employer name.
                    prev = loc_div.previous_sibling
                    employer = clean_text(prev) if prev else ""
                else:
                    employer = clean_text(h3.get_text(" ", strip=True))

        def get_info(test_id: str) -> str:
            el = item.find(attrs={"data-test": test_id})
            if not el:
                return ""
            strong = el.find("strong")
            if strong:
                return clean_text(strong.get_text(" ", strip=True))
            return clean_text(el.get_text(" ", strip=True))

        # Build a clean advert URL without the search page parameter.
        clean_url = urljoin(BASE_URL, relative)

        return {
            "title": clean_text(title_link.get_text()),
            "reference": reference,
            "url": clean_url,
            "employer": employer,
            "location": location,
            "salary": get_info("search-result-salary"),
            "date_posted": get_info("search-result-publicationDate"),
            "closing_date": get_info("search-result-closingDate"),
            "contract_type": get_info("search-result-jobType"),
            "working_pattern": get_info("search-result-workingPattern"),
        }

    def fetch_job_details(self, job_url: str) -> dict:
        """Fetch a job advert and extract details + supporting documents."""
        resp = self._get(job_url)
        soup = BeautifulSoup(resp.text, "html.parser")

        details = {
            "page_text": "",
            "documents": [],
        }

        # Main advert text is inside <main>.
        main = soup.find("main")
        if main:
            details["page_text"] = clean_text(main.get_text(" ", strip=True))

        # Structured fields from the Details panel.
        def detail_value(label: str) -> str:
            for dt in soup.find_all("dt"):
                if label.lower() in dt.get_text(strip=True).lower():
                    dd = dt.find_next_sibling("dd")
                    if dd:
                        return clean_text(dd.get_text(" ", strip=True))
            return ""

        details["pay_scheme"] = detail_value("Pay scheme")
        details["band"] = detail_value("Band")
        details["salary"] = detail_value("Salary")
        details["contract"] = detail_value("Contract")
        details["duration"] = detail_value("Duration")
        details["working_pattern"] = detail_value("Working pattern")
        details["reference_number"] = detail_value("Reference number")

        # Employer's website link (for trust-value drilling).
        details["employer_website"] = ""
        website_heading = soup.find("h3", id="employer_website_heading")
        if website_heading:
            website_p = website_heading.find_next_sibling("p", id="employer_website_url")
            if website_p:
                link = website_p.find("a", href=True)
                if link:
                    details["employer_website"] = urljoin(job_url, link["href"])
                else:
                    details["employer_website"] = clean_text(website_p.get_text(" ", strip=True))

        # Supporting documents are POST forms; collect CSRF + document id.
        docs_heading = soup.find("h3", id="supporting_documents_heading")
        if docs_heading:
            for form in docs_heading.find_all_next("form", method="post"):
                # Stop if we have reached the next content block.
                if form.find_previous(["h2", "h3"]) != docs_heading:
                    break
                csrf_inp = form.find("input", {"name": "_csrf"})
                doc_inp = form.find("input", {"name": "document"})
                submit = form.find("input", {"type": "submit"})
                if csrf_inp and doc_inp and submit:
                    raw_value = clean_text(submit.get("value", ""))
                    details["documents"].append({
                        "filename": parse_document_filename(raw_value),
                        "submit_value": raw_value,
                        "csrf": csrf_inp.get("value", ""),
                        "document_id": doc_inp.get("value", ""),
                        "submit_id": submit.get("id", ""),
                    })

        return details

    def download_document(self, job_url: str, doc: dict) -> Optional[bytes]:
        """Download a supporting document using its form fields."""
        if not doc.get("csrf") or not doc.get("document_id"):
            return None

        payload = {
            "_csrf": doc["csrf"],
            "document": doc["document_id"],
        }
        if doc.get("submit_id"):
            payload[doc["submit_id"]] = doc.get("submit_value") or doc["filename"]

        headers = {"Referer": job_url}
        resp = self._post(job_url, data=payload, headers=headers, allow_redirects=True)
        return resp.content

    def fetch_trust_website_summary(self, url: str, llm: Optional[LLMSummarizer] = None) -> dict:
        """
        Fetch the trust's public website and extract a short summary.

        Tries to locate an About Us / Values / Mission page from the homepage
        navigation and summarises that page; falls back to the homepage if no
        suitable page is found. If an LLM summariser is supplied it is used to
        produce the summary, otherwise the legacy extractive summariser is used.

        Returns a dict with keys: url, title, summary, error.
        """
        result = {"url": url, "title": "", "summary": "", "error": ""}
        if not url or not url.startswith("http"):
            result["error"] = "No valid trust website URL."
            return result

        def extract_page_text(page_soup: BeautifulSoup) -> str:
            main = (
                page_soup.find("main")
                or page_soup.find("div", class_=re.compile(r"content|main", re.I))
                or page_soup.find("body")
            )
            if main:
                return clean_text(main.get_text(" ", strip=True))
            return ""

        try:
            # Fetch the homepage first.
            resp = self._get(url, timeout=20)
            soup = BeautifulSoup(resp.text, "html.parser")
            result["title"] = clean_text(soup.title.string) if soup.title else ""

            # Look for a trust-values page linked from the homepage.
            # Priority: most specific values pages first, then general About Us.
            search_patterns = [
                r"our\s+values",
                r"values?\b",
                r"about\s+us",
                r"about\b",
                r"who\s+we\s+are",
                r"mission",
                r"vision",
            ]
            best_link = None
            best_score = len(search_patterns)  # lower is better

            for a in soup.find_all("a", href=True):
                text = clean_text(a.get_text(" ", strip=True)).lower()
                if not text:
                    continue
                for score, pattern in enumerate(search_patterns):
                    if re.search(pattern, text):
                        if score < best_score:
                            best_score = score
                            href = a["href"].strip()
                            # Ignore anchors/javascript on the same page.
                            if href.startswith("#") or href.lower().startswith("javascript:"):
                                continue
                            best_link = urljoin(url, href)
                        break  # only count the highest-priority match for this link

            # If we found a relevant page, fetch and use it.
            if best_link and best_link != url:
                try:
                    inner_resp = self._get(best_link, timeout=20)
                    inner_soup = BeautifulSoup(inner_resp.text, "html.parser")
                    text = extract_page_text(inner_soup)
                    if text:
                        result["url"] = best_link
                        result["title"] = clean_text(inner_soup.title.string) if inner_soup.title else result["title"]
                        result["summary"] = (
                            llm.summarize(text, "trust")
                            if llm and llm.is_configured()
                            else summarise_trust_text(text)
                        )
                        return result
                except Exception as inner_exc:
                    # Fall back to homepage summary.
                    result["error"] = f"Could not fetch values page ({best_link}): {inner_exc}"

            # Fallback: summarise the homepage.
            text = extract_page_text(soup)
            if text:
                result["summary"] = (
                    llm.summarize(text, "trust")
                    if llm and llm.is_configured()
                    else summarise_trust_text(text)
                )
            else:
                result["error"] = "Could not find page content."
        except Exception as exc:
            result["error"] = str(exc)

        return result


# ---------------------------------------------------------------------------
# Trust website summarisation
# ---------------------------------------------------------------------------
TRUST_KEY_PHRASES = [
    "trust", "hospital", "nhs", "care", "patients", "values", "mission",
    "vision", "about us", "our services", "excellence", "compassion",
    "respect", "dignity", "commitment", "quality", "safety", "wellbeing",
    "staff", "team", "award", "foundation trust", "healthcare", "community",
    "partnership", "innovation", "improvement", "integrity", "accountability",
    "kindness", "patient centred", "person centred", "our purpose",
]


def summarise_trust_text(text: str, max_sentences: int = 8) -> str:
    """Extractive summary focused on trust identity, values and services."""
    if not text:
        return "No content available."

    raw_sentences = re.split(r"(?<=[.!?])\s+", text.replace("\n", " "))
    sentences = [clean_text(s) for s in raw_sentences if len(s.split()) > 5]

    def score(sentence: str) -> int:
        lowered = sentence.lower()
        return sum(1 for phrase in TRUST_KEY_PHRASES if phrase in lowered)

    scored = [(i, score(s), s) for i, s in enumerate(sentences)]
    scored.sort(key=lambda x: x[1], reverse=True)
    top = scored[:max_sentences]
    top.sort(key=lambda x: x[0])
    return "\n".join(f"- {s}" for _, _, s in top)


# ---------------------------------------------------------------------------
# Document text extraction
# ---------------------------------------------------------------------------
def extract_text_from_bytes(data: bytes, filename: str) -> str:
    """Best-effort text extraction from PDF/DOC/DOCX bytes."""
    lowered = filename.lower()

    # PDF
    if lowered.endswith(".pdf") or data[:4] == b"%PDF":
        try:
            import PyPDF2
            reader = PyPDF2.PdfReader(io.BytesIO(data))
            parts = []
            for page in reader.pages:
                try:
                    parts.append(page.extract_text() or "")
                except Exception:
                    pass
            return "\n".join(parts)
        except Exception as exc:
            return f"[Could not extract PDF text: {exc}]"

    # DOCX (often mis-named .doc but is actually a zip)
    if lowered.endswith(".docx") or data[:2] == b"PK":
        try:
            import docx
            doc = docx.Document(io.BytesIO(data))
            return "\n".join(p.text for p in doc.paragraphs)
        except Exception as exc:
            return f"[Could not extract DOCX text: {exc}]"

    # Legacy .doc - try Microsoft Word via COM on Windows, then textract.
    if lowered.endswith(".doc"):
        temp_path = DOCS_DIR / f"_tmp_{hashlib.md5(data).hexdigest()}.doc"
        temp_path.write_bytes(data)
        try:
            try:
                import win32com.client
                word = win32com.client.Dispatch("Word.Application")
                word.Visible = False
                word.DisplayAlerts = False
                doc = word.Documents.Open(str(temp_path.resolve()))
                text = doc.Content.Text
                doc.Close(SaveChanges=False)
                word.Quit()
                return text
            except Exception:
                pass

            try:
                import textract
                text = textract.process(str(temp_path), extension="doc").decode("utf-8", errors="ignore")
                return text
            except Exception:
                pass
        finally:
            temp_path.unlink(missing_ok=True)

    return "[Document format not supported for automatic text extraction. File saved for manual review.]"


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------
def write_html_report(jobs: list, keyword: str, output_path: Path) -> None:
    """Generate a styled HTML report for browser reading."""

    def html_escape(text: str) -> str:
        return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def para(text: str) -> str:
        return f"<p>{html_escape(text)}</p>" if text else ""

    def bullet_list(text: str) -> str:
        if not text:
            return ""
        items = [line[2:].strip() for line in text.splitlines() if line.strip().startswith("- ")]
        if not items:
            return para(text)
        return "<ul>" + "".join(f"<li>{html_escape(item)}</li>" for item in items) + "</ul>"

    head = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NHS Jobs Search Report</title>
    <style>
        :root { --nhs-blue: #005eb8; --nhs-dark-blue: #003087; --nhs-light-grey: #f0f4f5; }
        body { font-family: Arial, Helvetica, sans-serif; line-height: 1.6; max-width: 900px; margin: 0 auto; padding: 20px; color: #212b32; }
        h1 { color: var(--nhs-blue); border-bottom: 4px solid var(--nhs-blue); padding-bottom: 10px; }
        h2 { color: var(--nhs-dark-blue); margin-top: 40px; border-left: 6px solid var(--nhs-blue); padding-left: 12px; }
        h3 { color: var(--nhs-blue); margin-top: 28px; }
        h4 { color: #4c6272; margin-top: 20px; }
        .meta { background: var(--nhs-light-grey); padding: 16px; border-radius: 4px; margin-bottom: 20px; }
        .meta ul { list-style: none; padding: 0; margin: 0; }
        .meta li { margin-bottom: 6px; }
        .meta a { color: var(--nhs-blue); }
        .trust { background: #fff9c4; padding: 14px; border-radius: 4px; margin-top: 20px; }
        ul { padding-left: 20px; }
        li { margin-bottom: 8px; }
        hr { border: 0; border-top: 1px solid #d8dde0; margin: 40px 0; }
        .section-heading { font-weight: bold; color: #4c6272; margin-top: 14px; }
        table.toc { width: 100%; border-collapse: collapse; margin-bottom: 20px; }
        table.toc th, table.toc td { text-align: left; padding: 10px; border-bottom: 1px solid #d8dde0; }
        table.toc th { background: var(--nhs-light-grey); }
        table.toc a { color: var(--nhs-blue); text-decoration: none; }
        table.toc a:hover { text-decoration: underline; }
    </style>
</head>
<body>
"""

    body_lines = [
        "<h1>NHS Jobs Search Report</h1>",
        f'<p><strong>Search term:</strong> {html_escape(keyword)}</p>',
        f'<p><strong>Jobs found:</strong> {len(jobs)}</p>',
    ]

    # Table of contents with clickable links to each job detail.
    if jobs:
        body_lines.append("<h2>Job list</h2>")
        body_lines.append('<table class="toc"><thead><tr><th>#</th><th>Job title</th><th>Trust</th><th>Closing date</th></tr></thead><tbody>')
        for idx, job in enumerate(jobs, 1):
            body_lines.append(
                f'<tr>'
                f'<td>{idx}</td>'
                f'<td><a href="#job-{idx}">{html_escape(job.get("title", ""))}</a></td>'
                f'<td>{html_escape(job.get("employer", ""))}</td>'
                f'<td>{html_escape(job.get("closing_date", ""))}</td>'
                f'</tr>'
            )
        body_lines.append("</tbody></table>")
        body_lines.append("<hr>")

    for idx, job in enumerate(jobs, 1):
        body_lines.append(f'<a id="job-{idx}"></a>')
        body_lines.append(f'<h2>{idx}. {html_escape(job["title"])}</h2>')
        body_lines.append('<div class="meta">')
        body_lines.append("<ul>")
        body_lines.append(f'<li><strong>Reference:</strong> {html_escape(job.get("reference", ""))}</li>')
        body_lines.append(f'<li><strong>Employer:</strong> {html_escape(job.get("employer", ""))}</li>')
        body_lines.append(f'<li><strong>Location:</strong> {html_escape(job.get("location", ""))}</li>')
        body_lines.append(f'<li><strong>Salary:</strong> {html_escape(job.get("salary", ""))}</li>')
        body_lines.append(f'<li><strong>Contract type:</strong> {html_escape(job.get("contract_type", ""))}</li>')
        body_lines.append(f'<li><strong>Working pattern:</strong> {html_escape(job.get("working_pattern", ""))}</li>')
        body_lines.append(f'<li><strong>Closing date:</strong> {html_escape(job.get("closing_date", ""))}</li>')
        advert_url = job.get("url", "")
        body_lines.append(f'<li><strong>Advert URL:</strong> <a href="{html_escape(advert_url)}" target="_blank">{html_escape(advert_url)}</a></li>')
        body_lines.append("</ul>")
        body_lines.append("</div>")

        body_lines.append("<h3>Key points from the advert</h3>")
        details = job.get("details", {})
        page_summary = details.get("page_summary") or summarise_text(details.get("page_text", ""))
        body_lines.append(bullet_list(page_summary))

        docs = job.get("details", {}).get("documents", [])
        body_lines.append("<h3>Supporting documents</h3>")
        if docs:
            for doc in docs:
                filename = doc.get("filename", "Unknown")
                body_lines.append(f'<h4>{html_escape(filename)}</h4>')
                doc_summary = doc.get("summary")
                doc_text = doc.get("text", "")
                if doc_summary:
                    body_lines.append(bullet_list(doc_summary))
                elif doc_text:
                    sections = extract_sections(doc_text)
                    if sections:
                        for heading, section_text in sections.items():
                            if section_text:
                                body_lines.append(f'<div class="section-heading">{html_escape(heading)}</div>')
                                body_lines.append(bullet_list(summarise_text(section_text, max_sentences=8)))
                    else:
                        body_lines.append(bullet_list(summarise_text(doc_text, max_sentences=10)))
                else:
                    body_lines.append("<p><em>No text could be extracted from this document.</em></p>")
        else:
            body_lines.append("<p><em>No supporting documents listed.</em></p>")

        trust = job.get("trust_summary", {})
        if trust:
            body_lines.append('<div class="trust">')
            body_lines.append("<h3>About the trust</h3>")
            trust_url = trust.get("url", "")
            trust_title = trust.get("title", "")
            if trust_title:
                body_lines.append(f"<p><strong>{html_escape(trust_title)}</strong></p>")
            if trust_url:
                body_lines.append(f'<p>Website: <a href="{html_escape(trust_url)}" target="_blank">{html_escape(trust_url)}</a></p>')
            trust_summary = trust.get("summary", "")
            trust_error = trust.get("error", "")
            if trust_summary:
                body_lines.append(bullet_list(trust_summary))
            elif trust_error:
                body_lines.append(f"<p><em>Could not retrieve trust website: {html_escape(trust_error)}</em></p>")
            body_lines.append("</div>")

        body_lines.append("<hr>")

    html = head + "\n".join(body_lines) + "\n</body>\n</html>\n"
    output_path.write_text(html, encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def load_config(path: Path) -> dict:
    config = configparser.ConfigParser()
    if path.exists():
        config.read(path)
    return {
        "url": config.get("Login", "url", fallback=urljoin(BASE_URL, SEARCH_PATH)),
        "email": config.get("Login", "email", fallback=""),
        "password": config.get("Login", "pwd", fallback=""),
        "search_url": config.get("Search", "url", fallback=""),
        "search_keyword": config.get("Search", "keyword", fallback=""),
        "exclude_terms": config.get("Search", "exclude", fallback=""),
        "llm_provider": config.get("LLM", "provider", fallback=""),
        "llm_api_key": config.get("LLM", "api_key", fallback=""),
        "llm_model": config.get("LLM", "model", fallback=""),
    }


def main():
    cfg = load_config(CONFIG_PATH)
    client = NHSJobsClient()
    llm = LLMSummarizer(
        cfg.get("llm_provider", ""),
        cfg.get("llm_api_key", ""),
        cfg.get("llm_model", ""),
    )
    if llm.is_configured():
        print(f"LLM summarisation enabled: {cfg.get('llm_provider')} / {cfg.get('llm_model')}")

    # Optional login (searching is public, so this is not required).
    if cfg.get("email") and cfg.get("password"):
        print("Attempting optional login...")
        logged_in = client.login(cfg["email"], cfg["password"])
        print("Logged in:" if logged_in else "Login failed or not required; continuing with public search.")

    keyword = cfg.get("search_keyword") or os.environ.get("SEARCH_KEYWORD") or "rotational physiotherapist"
    search_url = cfg.get("search_url") or os.environ.get("SEARCH_URL") or ""
    exclude_terms = parse_exclude_terms(
        cfg.get("exclude_terms") or os.environ.get("EXCLUDE_TERMS") or ""
    )
    max_jobs = int(os.environ.get("MAX_JOBS", "10"))
    max_candidates = int(os.environ.get("MAX_CANDIDATES", "50"))

    display_term = keyword if not search_url else (search_url.split("?", 1)[0] if "?" in search_url else search_url)
    print(f"Searching for: {display_term} (keyword filter: {keyword})")
    if exclude_terms:
        print(f"Excluding adverts containing: {', '.join(exclude_terms)}")

    candidates = client.search_jobs(
        keyword,
        search_url=search_url,
        max_candidates=max_candidates,
    )
    print(f"Found {len(candidates)} candidate jobs; checking details...")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    seen_refs = load_seen_references(SEEN_REFS_PATH)
    if seen_refs:
        print(f"{len(seen_refs)} job reference(s) already processed in previous runs will be skipped.")

    # Load previously saved jobs so the report stays cumulative across runs.
    existing_jobs = {j.get("reference", ""): j for j in load_existing_jobs(JSON_PATH)}

    # Backfill title/trust for older seen-references entries that only stored refs.
    for ref, entry in seen_refs.items():
        if ref in existing_jobs:
            job = existing_jobs[ref]
            if not entry.get("title"):
                entry["title"] = job.get("title", "")
            if not entry.get("trust"):
                entry["trust"] = job.get("employer", "")

    jobs = []
    for i, job in enumerate(candidates, 1):
        if len(jobs) >= max_jobs:
            break
        ref = job.get("reference", "")
        if ref and ref in seen_refs:
            print(f"[{i}/{len(candidates)}] Skipping {ref} - already processed.")
            continue
        print(f"[{i}/{len(candidates)}] Processing {ref} - {job['title']}")
        try:
            details = client.fetch_job_details(job["url"])
            combined_text = f"{job.get('title', '')} {details.get('page_text', '')}"

            # Strict filters to ensure we only report rotational Band 5 physiotherapy posts.
            if not is_physiotherapy(combined_text):
                print(f"  Skipping {job['reference']}: not physiotherapy-related.")
                continue
            if not is_band_5(combined_text):
                print(f"  Skipping {job['reference']}: not Band 5.")
                continue
            if is_unwanted_band(combined_text):
                print(f"  Skipping {job['reference']}: mentions a higher band.")
                continue
            if not is_rotational(combined_text):
                print(f"  Skipping {job['reference']}: not rotational.")
                continue
            # Exclusion terms are matched against the job title so that general
            # rotational posts which merely mention an excluded specialty in the
            # body text are not removed.
            if contains_excluded_term(job.get("title", ""), exclude_terms):
                matched = [t for t in exclude_terms if t in job.get("title", "").lower()]
                print(f"  Skipping {job['reference']}: excluded term(s) in title: {', '.join(matched)}.")
                continue

            job["details"] = details

            # Generate an LLM summary of the advert page when configured.
            page_text = details.get("page_text", "")
            if page_text and llm.is_configured():
                print("  Generating LLM summary of advert...")
                details["page_summary"] = llm.summarize(page_text, "advert")

            # Fetch trust website summary if a URL is present.
            trust_url = details.get("employer_website", "")
            if trust_url:
                print(f"  Fetching trust website: {trust_url}")
                job["trust_summary"] = client.fetch_trust_website_summary(trust_url, llm=llm)
            else:
                job["trust_summary"] = {"url": "", "title": "", "summary": "", "error": "No website listed."}

            jobs.append(job)
            if ref:
                seen_refs[ref] = {
                    "reference": ref,
                    "date_scraped": datetime.datetime.now().isoformat(timespec="seconds"),
                    "title": job.get("title", ""),
                    "trust": job.get("employer", ""),
                }

            for doc in details.get("documents", []):
                try:
                    data = client.download_document(job["url"], doc)
                    if not data:
                        continue
                    safe_name = re.sub(r"[^\w\-. ]", "_", doc["filename"])
                    safe_name = re.sub(r"[ _]+", "_", safe_name).strip("_.")
                    safe_name = safe_name or f"doc_{doc['document_id']}"
                    doc_path = DOCS_DIR / f"{job['reference']}_{safe_name}"
                    doc_path.write_bytes(data)
                    doc["saved_path"] = str(doc_path.relative_to(BASE_DIR))
                    doc["text"] = extract_text_from_bytes(data, doc["filename"])
                    if doc["text"] and llm.is_configured():
                        print(f"  Generating LLM summary of {doc['filename']}...")
                        doc["summary"] = llm.summarize(doc["text"], "document")
                except Exception as exc:
                    doc["error"] = str(exc)
                    print(f"  Could not download document {doc.get('filename')}: {exc}", file=sys.stderr)
        except Exception as exc:
            print(f"  Could not fetch details for {job['reference']}: {exc}", file=sys.stderr)
            job["details"] = {"error": str(exc)}

    print(f"Kept {len(jobs)} new matching job(s).")

    # Persist references so future runs skip duplicates.
    save_seen_references(SEEN_REFS_PATH, seen_refs)

    # Merge new jobs into existing data so reports are cumulative.
    for job in jobs:
        ref = job.get("reference", "")
        if ref:
            existing_jobs[ref] = job
    all_jobs = list(existing_jobs.values())

    # Save structured data.
    JSON_PATH.write_text(json.dumps(all_jobs, indent=2, ensure_ascii=False), encoding="utf-8")

    # Save HTML report with YYYYMMDD suffix for easy reference.
    today = datetime.datetime.now().strftime("%Y%m%d")
    html_report_path = OUTPUT_DIR / f"jobs_report_{today}.html"
    write_html_report(all_jobs, keyword, html_report_path)

    print(f"\nDone. Cumulative unique jobs in report: {len(all_jobs)}")
    print(f"HTML report: {html_report_path}")
    print(f"Raw data:        {JSON_PATH}")
    print(f"Documents:       {DOCS_DIR}")
    print(f"Seen references: {SEEN_REFS_PATH}")


if __name__ == "__main__":
    main()
