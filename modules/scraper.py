#!/usr/bin/env python3
"""
Phase 1: Scraper & Normalization Engine
=======================================

Hybrid ingestion strategy
-------------------------
1. DIRECT ATS TARGETING (primary, no bot-blocks):
   Corporate career portals run on a handful of ATS vendors that expose the same
   public JSON endpoints their own career pages call. We hit those directly:
       Workday (CXS), Greenhouse, Lever, Ashby, SmartRecruiters
   No HTML scraping, no headless browser, no search-engine dorking.

2. AGGREGATORS (secondary) via `python-jobspy`:
   Indeed, Naukri (India only) and LinkedIn by default.
   LinkedIn uses its PUBLIC guest job pages only - never your LinkedIn account - so the worst
   case is a temporary block of your internet address for a few hours, never a ban on your profile.
   Volume is kept modest and searches are spaced out (settings in config.toml, [scraper]).

Filters applied before anything is analysed
-------------------------------------------
- Title words to exclude (e.g. intern, apprentice) come from the profile pack:
  profiles/<name>/domain.toml -> exclude_title_words
- Duplicates are removed across sources: same description, same link, or same company + title.
  When two copies exist, the one with the longer description is kept.

Company resolution
------------------
- Built-in registry (DEFAULT_REGISTRY) of known tenants.
- Override/extend with data/ats_registry.json:
      {"airbus": "https://ag.wd3.myworkdayjobs.com/en-US/Airbus",
       "acme":   "https://boards.greenhouse.io/acme"}
  Paste any careers URL; the ATS type is detected from it.
- If a company isn't registered, we probe Greenhouse/Lever/Ashby/SmartRecruiters
  with slug guesses derived from the company name.

Install:
    pip install requests pydantic beautifulsoup4 python-jobspy pandas

Run:
    python modules/scraper.py
    python modules/scraper.py --role "Design Engineer" --location Bengaluru
    python modules/scraper.py --sites indeed,naukri          # leave LinkedIn out for one run
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import math
import os
import re
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime
from typing import Callable, Optional
from urllib.parse import urlparse

import requests
from pydantic import BaseModel, ValidationError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from bs4 import BeautifulSoup
except ImportError:  # graceful fallback; bs4 is recommended
    BeautifulSoup = None  # type: ignore

log = logging.getLogger("scraper")

# --------------------------------------------------------------------------- #
# Data contract (Phase 1 schema) - do not change without updating Phase 2
# --------------------------------------------------------------------------- #

# Single source of truth is schemas/models.py. `python modules/scraper.py` puts
# modules/ (not the project root) on sys.path, so add the root before importing.
_ROOT_DIR = Path(__file__).resolve().parent.parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
try:
    from schemas.models import NormalizedJob
except ImportError:  # standalone fallback; identical to the contract
    class NormalizedJob(BaseModel):  # type: ignore[no-redef]
        job_id: str
        company: str
        title: str
        location: str
        application_url: str
        raw_description: str
        content_hash: str

from modules.settings import CFG, DATA_DIR, profile_dir  # noqa: E402

# --------------------------------------------------------------------------- #
# Paths / config
# --------------------------------------------------------------------------- #

ROOT = _ROOT_DIR
OUTPUT_FILE = DATA_DIR / "pending_jobs.json"
USER_REGISTRY_FILE = DATA_DIR / "ats_registry.json"

_S = CFG.get("scraper", {})
MIN_DESCRIPTION_CHARS = 80
FRESHNESS_DAYS = int(_S.get("freshness_days", 30))  # aggregator results older than this are not requested
LINKEDIN_MAX = int(_S.get("linkedin_max_results", 25))      # per LinkedIn search (one search per location)
LINKEDIN_PAUSE_S = float(_S.get("linkedin_pause_seconds", 8))  # average wait before each LinkedIn request
LINKEDIN_BREAK_EVERY = [int(x) for x in _S.get("linkedin_break_every", [6, 10])]          # a longer break after this many requests
LINKEDIN_BREAK_S = [float(x) for x in _S.get("linkedin_break_seconds", [30, 75])]         # ...lasting this long
LINKEDIN_ENGINE = str(_S.get("linkedin_engine", "builtin"))
LINKEDIN_BUDGET = int(_S.get("linkedin_request_budget", 45))     # LinkedIn requests per run (it blocks at ~40-50)
LINKEDIN_WATCH_PER_RUN = int(_S.get("linkedin_watch_per_run", 2))  # watched companies given LinkedIn time per run
STATE_FILE = DATA_DIR / "scraper_state.json"     # remembers which watched companies LinkedIn covered last
LEADS_FILE = DATA_DIR / "leads.json"             # jobs found without a usable description (paste it in later)
LAST_RUN_FILE = DATA_DIR / "last_scrape.json"    # summary of the latest run (for the dashboard)
LEDGER_FILE = DATA_DIR / "application_ledger.csv"
DONE_STATUSES = {"applied", "interview", "offer", "rejected", "archived", "withdrawn"}    # "builtin" (our polite fetcher) or "jobspy"
# LinkedIn's own experience filter: 1 internship, 2 entry, 3 associate, 4 mid-senior, 5 director, 6 executive
LINKEDIN_LEVELS = [int(x) for x in _S.get("linkedin_experience_levels", [])]
DROPPED_FILE = DATA_DIR / "dropped_jobs.json"
TITLE_MODE = "broad"  # "strict": most role words must be in the title; "broad": any one is enough
COUNTRY_FILTER: Optional[str] = None  # only used when no specific location is given
EXCLUDE_TITLE_RE: Optional[re.Pattern] = None  # built from the profile's domain.toml in main()
WATCH_MAX = int(_S.get("watch_max_results", 10))   # per watched company, per job board
ALIASES: dict[str, list[str]] = {}                  # company searched -> names that count as the same company

# Every job that is rejected anywhere in the pipeline is recorded here with a reason.
DROPPED: list[dict] = []


def note_drop(stage: str, source: str, company: str, title: str, location: str,
              url: str, reason: str, detail: str = "") -> None:
    DROPPED.append({"reason": reason, "title": title, "company": company, "location": location,
                    "url": url, "detail": detail, "stage": stage, "source": source})


HTTP_TIMEOUT = 30
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Known tenants. Values are careers URLs (ATS auto-detected). Treat these as
# starting points: tenants occasionally rename sites. Failures are logged and
# skipped, and you can override any entry in data/ats_registry.json.
DEFAULT_REGISTRY: dict[str, str] = {
    # Workday
    "airbus": "https://ag.wd3.myworkdayjobs.com/en-US/Airbus",
    # SAP SuccessFactors career sites: write "successfactors:" + the careers address
    "wipro": "successfactors:https://careers.wipro.com",
    "hcltech": "successfactors:https://careers.hcltech.com",
}
SKIP_PORTALS = {x.lower() for x in _S.get("skip_portals", [])}   # registry entries to ignore (config.toml)
PROFILE: Optional[str] = None   # profile folder (set in main); its companies.json adds portals and watch entries

LOCATION_ALIASES = {
    "bengaluru": ["bangalore", "bengaluru"],
    "bangalore": ["bangalore", "bengaluru"],
    "gurgaon": ["gurgaon", "gurugram"],
    "gurugram": ["gurgaon", "gurugram"],
    "mumbai": ["mumbai", "bombay"],
    "chennai": ["chennai", "madras"],
    "pune": ["pune"],
}

INDIA_HINTS = {
    "india", "bengaluru", "bangalore", "mumbai", "delhi", "hyderabad", "chennai",
    "pune", "gurgaon", "gurugram", "noida", "kolkata", "ahmedabad", "kochi",
}

STOPWORDS = {"a", "an", "and", "for", "of", "the", "in", "at", "to", "with"}


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #


def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=16)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*"})
    return s


SESSION = make_session()


def get_json(url: str, **kw) -> Optional[object]:
    r = SESSION.get(url, timeout=HTTP_TIMEOUT, **kw)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #


def html_to_text(raw: str | None) -> str:
    """Convert HTML (possibly entity-escaped, as Greenhouse does) to clean text."""
    if not raw:
        return ""
    raw = html.unescape(raw) if "&lt;" in raw else raw
    if BeautifulSoup is not None:
        soup = BeautifulSoup(raw, "html.parser")
        for t in soup(["script", "style"]):
            t.decompose()
        for br in soup.find_all("br"):
            br.replace_with("\n")
        for li in soup.find_all("li"):
            li.insert_before("\n- ")
        # Newline after each block element; inline tags (<b>, <span>) stay inline.
        for blk in soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "tr", "li"]):
            blk.append("\n")
        text = soup.get_text()
    else:
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", raw)
        text = re.sub(r"<[^>]+>", "", text)
        text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_for_hash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def role_tokens(role: str) -> list[str]:
    toks = re.findall(r"[a-z0-9+#.]+", role.lower())
    return [t for t in toks if t not in STOPWORDS]


def title_matches(title: str, role: str) -> bool:
    toks = role_tokens(role)
    if not toks:
        return True
    t = title.lower()
    hits = sum(1 for tok in toks if tok in t)
    if TITLE_MODE == "broad":
        return hits >= 1  # the later gap-analysis gate (Phase 2/3) judges real relevance
    need = len(toks) if len(toks) <= 2 else math.ceil(0.6 * len(toks))
    return hits >= need


def _cnorm(name: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9& ]", " ", (name or "").lower()).split())


def company_matches(found: str, wanted: str) -> bool:
    """Does the company on a listing match the company we searched for (or one of its aliases)?"""
    f = _cnorm(found)
    names = [wanted, *ALIASES.get(wanted, [])]
    return any(_cnorm(n) and re.search(rf"(?<![a-z0-9]){re.escape(_cnorm(n))}(?![a-z0-9])", f) for n in names)


def excluded_title_word(title: str) -> Optional[str]:
    """The excluded word found in a job title (from domain.toml), or None."""
    if EXCLUDE_TITLE_RE is None:
        return None
    m = EXCLUDE_TITLE_RE.search(title or "")
    return m.group(0) if m else None


def country_matches(job_loc: str, country: Optional[str]) -> bool:
    if not country or not job_loc.strip():
        return True  # no country wanted, or job location unknown: keep
    jl = job_loc.lower()
    hints = {country.lower()} | {h for c, hs in COUNTRY_HINTS.items() if c.lower() == country.lower() for h in hs}
    return any(h in jl for h in hints)


def location_matches(job_loc: str, wanted: str) -> bool:
    if not wanted.strip():
        return country_matches(job_loc, COUNTRY_FILTER)
    if not job_loc.strip():
        return True  # unknown location: keep, let Phase 2/3 decide
    jl = job_loc.lower()
    for part in re.split(r"[,/]", wanted.lower()):
        part = part.strip()
        if not part:
            continue
        for alias in LOCATION_ALIASES.get(part, [part]):
            if alias in jl:
                return True
    return False


def reject_reason(title: str, loc: str, role: str, location: str) -> Optional[str]:
    if not title_matches(title, role):
        return "title_mismatch"
    if not location_matches(loc, location):
        return "location_mismatch"
    return None


# --------------------------------------------------------------------------- #
# Raw record (pre-validation)
# --------------------------------------------------------------------------- #


def raw_record(source: str, native_id: str, company: str, title: str,
               location: str, url: str, description: str) -> dict:
    return {
        "source": source,
        "native_id": str(native_id),
        "company": company,
        "title": (title or "").strip(),
        "location": (location or "").strip(),
        "url": (url or "").strip(),
        "description": description or "",
    }


# --------------------------------------------------------------------------- #
# ATS adapters. Each returns a list of raw records already filtered by role and
# location, with full descriptions fetched.
# --------------------------------------------------------------------------- #

Target = dict  # {"ats": str, ...params}


def ats_from_url(url: str) -> Optional[Target]:
    """Detect ATS type + params from any careers URL."""
    if url.lower().startswith("successfactors:"):
        base = url.split(":", 1)[1].strip().rstrip("/")
        return {"ats": "successfactors", "base": base} if base.startswith("http") else None
    u = urlparse(url if "://" in url else "https://" + url)
    host, parts = u.netloc.lower(), [p for p in u.path.split("/") if p]

    if host.endswith("myworkdayjobs.com") or host.endswith("myworkdaysite.com"):
        tenant = host.split(".")[0]
        parts = [p for p in parts if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", p)]
        if host.endswith("myworkdaysite.com") and len(parts) >= 3 and parts[0] == "recruiting":
            tenant, site = parts[1], parts[2]
        elif parts:
            site = parts[0]
        else:
            return None
        return {"ats": "workday", "host": host, "tenant": tenant, "site": site}
    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io", "boards.eu.greenhouse.io") and parts:
        return {"ats": "greenhouse", "token": parts[0]}
    if host.endswith("jobs.lever.co") and parts:
        return {"ats": "lever", "slug": parts[0], "eu": host.endswith("eu.lever.co")}
    if host == "jobs.ashbyhq.com" and parts:
        return {"ats": "ashby", "name": parts[0]}
    if host.endswith("smartrecruiters.com") and parts:
        return {"ats": "smartrecruiters", "company_id": parts[0]}
    return None


def fetch_workday(t: Target, company: str, role: str, location: str, limit: int) -> list[dict]:
    base = f"https://{t['host']}/wday/cxs/{t['tenant']}/{t['site']}"
    candidates: list[dict] = []
    offset, total, scanned_cap = 0, None, 400
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    while offset < scanned_cap:
        body = {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": role}
        r = SESSION.post(base + "/jobs", json=body, headers=headers, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if total is None:  # Workday only reports total on the first page
            total = data.get("total", 0)
        postings = data.get("jobPostings", [])
        if not postings:
            break
        for p in postings:
            loc_txt = p.get("locationsText", "")
            wd_url = f"https://{t['host']}/{t['site']}{p.get('externalPath', '')}"
            multi = bool(re.search(r"\b\d+\s+locations?\b", loc_txt, re.I))  # e.g. "2 Locations": real cities unknown yet
            if not title_matches(p.get("title", ""), role):
                note_drop("portal_filter", "workday", company, p.get("title", ""), loc_txt, wd_url,
                          "title_mismatch", f"portal returned it for search '{role}' but title lacks the role words")
            elif excluded_title_word(p.get("title", "")):
                note_drop("portal_filter", "workday", company, p.get("title", ""), loc_txt, wd_url,
                          "excluded_title_word", f"title contains '{excluded_title_word(p.get('title', ''))}'")
            elif not (multi or location_matches(loc_txt, location)):
                note_drop("portal_filter", "workday", company, p.get("title", ""), loc_txt, wd_url,
                          "location_mismatch", f"wanted: {location or COUNTRY_FILTER or 'any'}")
            else:
                candidates.append(p)
        offset += 20
        if offset >= (total or 0) or len(candidates) >= limit:
            break
    for extra in candidates[limit:]:
        note_drop("portal_filter", "workday", company, extra.get("title", ""), extra.get("locationsText", ""),
                  f"https://{t['host']}/{t['site']}{extra.get('externalPath', '')}",
                  "beyond_max_per_source", f"--max {limit}")
    candidates = candidates[:limit]

    def detail(p: dict) -> Optional[dict]:
        path = p.get("externalPath", "")
        try:
            r = SESSION.get(base + path, headers={"Accept": "application/json"}, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            info = r.json().get("jobPostingInfo", {})
        except Exception as e:  # noqa: BLE001
            log.debug("workday detail failed %s: %s", path, e)
            note_drop("portal_detail", "workday", company, p.get("title", ""), p.get("locationsText", ""),
                      f"https://{t['host']}/{t['site']}{path}", "detail_fetch_failed", str(e)[:200])
            return None
        country_name = (info.get("country") or {}).get("descriptor", "") if isinstance(info.get("country"), dict) else ""
        all_locs = ", ".join([info.get("location") or ""] + list(info.get("additionalLocations") or []) + [country_name])
        if not location_matches(all_locs.strip(", ") or p.get("locationsText", ""), location):
            note_drop("portal_detail", "workday", company, p.get("title", ""), all_locs,
                      info.get("externalUrl") or f"https://{t['host']}/{t['site']}{path}",
                      "location_mismatch", f"multi-location job; real locations did not match: {location or COUNTRY_FILTER}")
            return None
        url = info.get("externalUrl") or f"https://{t['host']}/{t['site']}{path}"
        return raw_record(
            "workday", info.get("jobReqId") or path, company,
            info.get("title") or p.get("title", ""),
            info.get("location") or p.get("locationsText", ""),
            url, html_to_text(info.get("jobDescription")),
        )

    with ThreadPoolExecutor(max_workers=6) as ex:
        return [r for r in ex.map(detail, candidates) if r]


def fetch_greenhouse(t: Target, company: str, role: str, location: str, limit: int) -> list[dict]:
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{t['token']}/jobs", params={"content": "true"})
    if not data:
        return []
    out = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        why = reject_reason(j.get("title", ""), loc, role, location)
        if why:
            note_drop("portal_filter", "greenhouse", company, j.get("title", ""), loc, j.get("absolute_url", ""), why)
            continue
        out.append(raw_record("greenhouse", j["id"], company, j.get("title", ""), loc,
                              j.get("absolute_url", ""), html_to_text(j.get("content"))))
        if len(out) >= limit:
            break
    return out


def fetch_lever(t: Target, company: str, role: str, location: str, limit: int) -> list[dict]:
    host = "api.eu.lever.co" if t.get("eu") else "api.lever.co"
    data = get_json(f"https://{host}/v0/postings/{t['slug']}", params={"mode": "json"})
    if not isinstance(data, list):
        return []
    out = []
    for j in data:
        loc = (j.get("categories") or {}).get("location", "") or ""
        why = reject_reason(j.get("text", ""), loc, role, location)
        if why:
            note_drop("portal_filter", "lever", company, j.get("text", ""), loc, j.get("hostedUrl", ""), why)
            continue
        parts = [j.get("descriptionPlain", "")]
        for sec in j.get("lists", []):
            parts.append(sec.get("text", ""))
            parts.append(html_to_text(sec.get("content")))
        parts.append(j.get("additionalPlain", ""))
        out.append(raw_record("lever", j["id"], company, j.get("text", ""), loc,
                              j.get("hostedUrl", ""), "\n\n".join(p for p in parts if p)))
        if len(out) >= limit:
            break
    return out


def fetch_ashby(t: Target, company: str, role: str, location: str, limit: int) -> list[dict]:
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{t['name']}")
    if not data:
        return []
    out = []
    for j in data.get("jobs", []):
        loc = j.get("location", "") or ""
        why = reject_reason(j.get("title", ""), loc, role, location)
        if why:
            note_drop("portal_filter", "ashby", company, j.get("title", ""), loc, j.get("jobUrl", ""), why)
            continue
        desc = j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml"))
        out.append(raw_record("ashby", j.get("id", ""), company, j.get("title", ""), loc,
                              j.get("jobUrl", ""), desc))
        if len(out) >= limit:
            break
    return out


def fetch_smartrecruiters(t: Target, company: str, role: str, location: str, limit: int) -> list[dict]:
    cid = t["company_id"]
    base = f"https://api.smartrecruiters.com/v1/companies/{cid}/postings"
    candidates, offset = [], 0
    while offset < 300 and len(candidates) < limit:
        data = get_json(base, params={"q": role, "limit": 100, "offset": offset})
        if not data or not data.get("content"):
            break
        for p in data["content"]:
            loc_d = p.get("location") or {}
            loc = ", ".join(x for x in (loc_d.get("city"), loc_d.get("region"), loc_d.get("country")) if x)
            why = reject_reason(p.get("name", ""), loc, role, location)
            if why:
                note_drop("portal_filter", "smartrecruiters", company, p.get("name", ""), loc,
                          f"https://jobs.smartrecruiters.com/{cid}/{p.get('id', '')}", why)
            else:
                candidates.append((p, loc))
        offset += 100
        if offset >= data.get("totalFound", 0):
            break

    def detail(item) -> Optional[dict]:
        p, loc = item
        try:
            d = get_json(p["ref"]) or {}
        except Exception as e:  # noqa: BLE001
            log.debug("smartrecruiters detail failed: %s", e)
            note_drop("portal_detail", "smartrecruiters", company, p.get("name", ""), loc,
                      f"https://jobs.smartrecruiters.com/{cid}/{p.get('id', '')}", "detail_fetch_failed", str(e)[:200])
            return None
        secs = ((d.get("jobAd") or {}).get("sections")) or {}
        text = "\n\n".join(html_to_text((secs.get(k) or {}).get("text"))
                           for k in ("jobDescription", "qualifications", "additionalInformation"))
        url = d.get("postingUrl") or d.get("applyUrl") or f"https://jobs.smartrecruiters.com/{cid}/{p['id']}"
        return raw_record("smartrecruiters", p["id"], company, p.get("name", ""), loc, url, text)

    with ThreadPoolExecutor(max_workers=6) as ex:
        return [r for r in ex.map(detail, candidates[:limit]) if r]

def parse_sf_search(page_html: str) -> list[dict]:
    """Job rows from a SuccessFactors career-site search page."""
    soup = BeautifulSoup(page_html, "html.parser")
    out, seen = [], set()
    rows = soup.select("tr.data-row") or soup.select("li.job-tile") or soup.select("[data-row]")
    for row in rows:
        a = row.select_one("a.jobTitle-link") or row.select_one("a[href*='/job/']")
        if not a or not a.get("href") or a["href"] in seen:
            continue
        seen.add(a["href"])
        loc = row.select_one(".jobLocation") or row.select_one("[class*='location']")
        out.append({"title": a.get_text(" ", strip=True), "href": a["href"],
                    "location": re.sub(r"\s+", " ", loc.get_text(" ", strip=True)) if loc else ""})
    return out


def parse_sf_job(page_html: str) -> str:
    soup = BeautifulSoup(page_html, "html.parser")
    el = (soup.select_one("span.jobdescription") or soup.select_one("div.jobdescription")
          or soup.select_one("[itemprop='description']") or soup.select_one("div.job"))
    if el:
        return html_to_text(str(el))
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and data.get("description"):
            return html_to_text(data["description"])
    return ""


def parse_sf_rss(xml_text: str) -> list[dict]:
    """Jobs from a SuccessFactors career-site RSS feed (the official public feed behind its search page)."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_text.strip().encode("utf-8"))
    except ET.ParseError:
        return []
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = html_to_text(item.findtext("description") or "")
        loc = ""
        m = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", title)       # "Design Engineer (Bengaluru, KA, IN, 560001)"
        if m:
            title, loc = m.group(1).strip(), m.group(2).strip()
        if title and link:
            out.append({"title": title, "url": link, "location": loc, "description": desc})
    return out


SF_FEED_COUNTS: dict[str, int] = {}   # feed -> number of jobs it listed (shown by --test-portal)
_SF_PLACEHOLDER = re.compile(r"^\s*no jobs currently available", re.I)
COUNTRY_CODES = {"IN": "India", "US": "USA", "GB": "UK", "UK": "UK", "FR": "France", "DE": "Germany",
                 "ES": "Spain", "NL": "Netherlands", "IT": "Italy", "IE": "Ireland", "CA": "Canada"}


def _sf_feed(base: str, kw: str, headers: dict, label: str) -> list[dict]:
    """All jobs of one feed search (20 per page, up to 100), without the 'no jobs' placeholder."""
    feed, seen_links = [], set()
    for startrow in range(0, 100, 20):
        r = SESSION.get(f"{base}/services/rss/job/", params={"locale": "en_US", "keywords": kw, "startrow": startrow},
                        headers=headers, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        page = [j for j in parse_sf_rss(r.text) if j["url"] not in seen_links and not _SF_PLACEHOLDER.match(j["title"])]
        if not page:
            break
        seen_links.update(j["url"] for j in page)
        feed += page
        if len(page) < 20:
            break
    SF_FEED_COUNTS[label] = len(feed)
    return feed


def _city_in_text(text: str, wanted: list[str]) -> Optional[str]:
    t = (text or "").lower()
    for w in wanted:
        for alias in LOCATION_ALIASES.get(w.lower(), [w.lower()]):
            if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", t):
                return w
    return None


def fetch_successfactors(t: Target, company: str, role: str, location: str, limit: int) -> list[dict]:
    """SuccessFactors career sites fill their search page with JavaScript, so we read the RSS feed the
    page itself advertises. Some sites (e.g. HCLTech) cannot search by city in the feed and list only the
    country ("(IN)"); then we search without a city and find the city in the job text ourselves."""
    base = t["base"]
    headers = {"Accept": "application/rss+xml,application/xml,text/xml,text/html",
               "Accept-Language": "en-US,en;q=0.9"}
    wanted = split_locations(location)
    spellings = list(dict.fromkeys(v for w in wanted for v in LOCATION_ALIASES.get(w.lower(), [w])))
    feed: list[dict] = []
    for loc in spellings:                     # e.g. Bengaluru AND Bangalore: sites label cities differently
        feed += _sf_feed(base, f"({role}) AND locationSearch:({loc})", headers, f"{company} / {loc}")
    if not feed:                              # city search found nothing: search by role only, filter below
        feed = _sf_feed(base, f"({role})", headers, f"{company} / any location (city checked by us)")

    candidates: list[dict] = []
    for job in feed:
        if any(c["url"] == job["url"] for c in candidates):
            continue
        word = excluded_title_word(job["title"])
        if not title_matches(job["title"], role):
            note_drop("portal_filter", "successfactors", company, job["title"], job["location"], job["url"],
                      "title_mismatch", f"title lacks the words of '{role}'")
            continue
        if word:
            note_drop("portal_filter", "successfactors", company, job["title"], job["location"], job["url"],
                      "excluded_title_word", f"title contains '{word}'")
            continue
        code = job["location"].strip().upper()
        if code in COUNTRY_CODES:             # only a country is given, e.g. "(IN)"
            country = COUNTRY_CODES[code]
            if COUNTRY_FILTER and country.lower() != COUNTRY_FILTER.lower():
                note_drop("portal_filter", "successfactors", company, job["title"], job["location"], job["url"],
                          "location_mismatch", f"country {country}; wanted {COUNTRY_FILTER}")
                continue
            if wanted:
                city = _city_in_text(job["description"], wanted)
                if not city:                  # look on the job's own page
                    try:
                        r = SESSION.get(job["url"], headers={"Accept": "text/html"}, timeout=HTTP_TIMEOUT)
                        city = _city_in_text(BeautifulSoup(r.text, "html.parser").get_text(" "), wanted) if r.ok else None
                    except requests.RequestException:
                        city = None
                if not city:
                    note_drop("portal_filter", "successfactors", company, job["title"], job["location"], job["url"],
                              "location_mismatch", f"site gives only the country ({country}); "
                                                   f"{', '.join(wanted)} not mentioned in the job")
                    continue
                job = {**job, "location": f"{city}, {country}"}
            else:
                job = {**job, "location": country}
        elif not location_matches(job["location"], location):
            note_drop("portal_filter", "successfactors", company, job["title"], job["location"], job["url"],
                      "location_mismatch", f"wanted: {location or COUNTRY_FILTER or 'any'}")
            continue
        candidates.append(job)
    for extra in candidates[limit:]:
        note_drop("portal_filter", "successfactors", company, extra["title"], extra["location"], extra["url"],
                  "beyond_max_per_source", f"--max {limit}")
    candidates = candidates[:limit]

    def detail(c: dict) -> Optional[dict]:
        desc = c["description"]
        if len(desc) < 300:                       # feed gave only a teaser: read the job page
            try:
                r = SESSION.get(c["url"], headers={"Accept": "text/html"}, timeout=HTTP_TIMEOUT)
                r.raise_for_status()
                desc = parse_sf_job(r.text) or desc
            except Exception as e:  # noqa: BLE001
                note_drop("portal_detail", "successfactors", company, c["title"], c["location"], c["url"],
                          "detail_fetch_failed", str(e)[:200])
        m = re.search(r"/(\d{6,})/?$", c["url"].split("?")[0])
        return raw_record("successfactors", m.group(1) if m else c["url"], company, c["title"], c["location"],
                          c["url"], desc)

    with ThreadPoolExecutor(max_workers=3) as ex:
        return [r for r in ex.map(detail, candidates) if r]


ATS_FETCHERS: dict[str, Callable[..., list[dict]]] = {
    "workday": fetch_workday,
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "successfactors": fetch_successfactors,
}


# --------------------------------------------------------------------------- #
# Company resolution
# --------------------------------------------------------------------------- #


def load_registry() -> dict[str, Target]:
    merged = dict(DEFAULT_REGISTRY)
    try:   # companies you added on the control centre's Companies page
        from modules.companies import registry_entries
        merged.update(registry_entries(PROFILE))
    except Exception as e:  # noqa: BLE001
        log.debug("companies.json not read: %s", e)
    if USER_REGISTRY_FILE.exists():
        try:
            merged.update({k.lower(): v for k, v in json.loads(USER_REGISTRY_FILE.read_text()).items()})
        except Exception as e:  # noqa: BLE001
            log.warning("Could not read %s: %s", USER_REGISTRY_FILE, e)
    reg: dict[str, Target] = {}
    for name, url in merged.items():
        if name.lower() in SKIP_PORTALS or not str(url).strip():
            continue
        t = ats_from_url(url)
        if t:
            reg[name.lower()] = t
        else:
            log.warning("Registry entry '%s' has unrecognised URL: %s", name, url)
    return reg


def discover_targets(company: str) -> list[Target]:
    """Probe public ATS endpoints with slug guesses for unregistered companies."""
    base = re.sub(r"[^a-z0-9 ]", "", company.lower()).strip()
    slugs = {base.replace(" ", ""), base.replace(" ", "-"), base.replace(" ", "_")} - {""}
    found: list[Target] = []
    for slug in sorted(slugs):
        probes = [
            ({"ats": "greenhouse", "token": slug}, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"),
            ({"ats": "lever", "slug": slug, "eu": False}, f"https://api.lever.co/v0/postings/{slug}?mode=json"),
            ({"ats": "ashby", "name": slug}, f"https://api.ashbyhq.com/posting-api/job-board/{slug}"),
        ]
        for target, url in probes:
            try:
                r = SESSION.get(url, timeout=15)
                if r.status_code == 200 and r.text.strip() not in ("", "[]", "{}"):
                    found.append(target)
            except requests.RequestException:
                continue
        try:
            d = get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", params={"limit": 1})
            if d and d.get("totalFound", 0) > 0:
                found.append({"ats": "smartrecruiters", "company_id": slug})
        except requests.RequestException:
            pass
        if found:
            break
    return found


def run_ats(company_display: str, targets: list[Target], role: str, location: str, limit: int) -> list[dict]:
    out: list[dict] = []
    for t in targets:
        try:
            recs = ATS_FETCHERS[t["ats"]](t, company_display, role, location, limit)
            log.info("  [%s] %s -> %d match(es)", t["ats"], company_display, len(recs))
            out.extend(recs)
        except Exception as e:  # noqa: BLE001
            log.warning("  [%s] %s failed: %s", t["ats"], company_display, e)
            note_drop("portal", t["ats"], company_display, "", "", "", "portal_error", str(e)[:200])
    return out


def collect_ats(role: str, company: str, location: str, limit: int) -> list[dict]:
    registry = load_registry()
    jobs: list[dict] = []
    if company:
        key = company.lower().strip()
        matched = [k for k in registry if key == k or key in k or k in key]
        if matched:
            targets_by_company = {company: [registry[k] for k in matched]}
        else:
            log.info("'%s' not in registry; probing public ATS endpoints...", company)
            tg = discover_targets(company)
            if not tg:
                log.warning(
                    "No ATS found for '%s'. Add its careers URL to %s to enable direct scraping.",
                    company, USER_REGISTRY_FILE,
                )
            targets_by_company = {company: tg}
    else:
        targets_by_company = {k.title(): [v] for k, v in registry.items()}

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(run_ats, c, ts, role, location, limit) for c, ts in targets_by_company.items() if ts]
        for f in futs:
            jobs.extend(f.result())
    return jobs


# --------------------------------------------------------------------------- #
# Aggregators (python-jobspy)
# --------------------------------------------------------------------------- #

COUNTRY_HINTS: dict[str, set[str]] = {
    "India": INDIA_HINTS,
    "France": {"france", "paris", "toulouse", "lyon", "marseille", "bordeaux", "nantes", "lille"},
    "Germany": {"germany", "deutschland", "berlin", "munich", "münchen", "hamburg", "frankfurt", "cologne", "stuttgart"},
    "UK": {"uk", "united kingdom", "england", "london", "manchester", "edinburgh", "birmingham", "bristol"},
    "Spain": {"spain", "madrid", "barcelona", "valencia", "seville"},
    "Netherlands": {"netherlands", "amsterdam", "rotterdam", "eindhoven", "utrecht"},
    "Italy": {"italy", "milan", "rome", "turin"},
    "Ireland": {"ireland", "dublin"},
    "USA": {"usa", "united states", "new york", "san francisco", "seattle", "austin", "boston", "chicago"},
    "Canada": {"canada", "toronto", "vancouver", "montreal"},
}


def infer_country(location: str, override: str | None) -> Optional[str]:
    """Country used for Indeed. Returns None when it can't be inferred."""
    if override:
        return override
    loc = location.lower()
    for country, hints in COUNTRY_HINTS.items():
        if any(h in loc for h in hints):
            return country
    return None


def default_sites(country: Optional[str]) -> list[str]:
    """Only search boards that actually cover the region (Naukri is India-only)."""
    sites = ["indeed", "naukri"] if (country or "").lower() == "india" else ["indeed"]
    if _S.get("linkedin", True):
        sites.append("linkedin")
    return sites

# --------------------------------------------------------------------------- #
# LinkedIn (built-in, polite): public guest pages only, never your account
# --------------------------------------------------------------------------- #

LI_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
LI_POSTING = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
LI_HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml",
              "Accept-Language": "en-US,en;q=0.9", "Referer": "https://www.linkedin.com/jobs/search"}


class LinkedInBlocked(Exception):
    pass


class LinkedInBudgetSpent(LinkedInBlocked):
    """Not a block: we stop ourselves before LinkedIn would block us."""


_LI_STATE = {"since_break": 0, "next_break": None, "used": 0}
LI_RUN = {"blocked": False}
PARTIAL: list[dict] = []   # LinkedIn records read before a stop in the middle of a search


def _li_pause() -> None:
    """Wait like a person reading: a varied pause before every request, plus an occasional longer break."""
    import random
    st = _LI_STATE
    if st["next_break"] is None:
        st["next_break"] = random.randint(*LINKEDIN_BREAK_EVERY)
    if st["since_break"] >= st["next_break"]:
        secs = random.uniform(*LINKEDIN_BREAK_S)
        log.info("    (short break of %.0f seconds, like a person reading)", secs)
        time.sleep(secs)
        st["since_break"], st["next_break"] = 0, random.randint(*LINKEDIN_BREAK_EVERY)
    time.sleep(LINKEDIN_PAUSE_S * random.uniform(0.6, 1.5))
    st["since_break"] += 1


def _li_get(url: str, params: Optional[dict] = None) -> Optional[str]:
    """One polite request. Waits before every call; on 'too many requests' waits two minutes and tries once more.
    Stops (LinkedInBudgetSpent) once this run's request budget is used up."""
    for attempt in (1, 2):
        if _LI_STATE["used"] >= LINKEDIN_BUDGET:
            raise LinkedInBudgetSpent(f"budget of {LINKEDIN_BUDGET} requests used")
        _li_pause()
        _LI_STATE["used"] += 1
        r = requests.get(url, params=params, headers=LI_HEADERS, timeout=HTTP_TIMEOUT)  # no automatic retries
        if r.status_code == 200:
            return r.text
        if r.status_code == 404:
            return None
        if r.status_code == 429 and attempt == 1:
            log.info("    LinkedIn asked us to slow down; waiting 2 minutes...")
            time.sleep(120)
            continue
        if r.status_code in (429, 999):
            raise LinkedInBlocked(f"HTTP {r.status_code}")
        r.raise_for_status()
    return None


def parse_linkedin_cards(page_html: str) -> list[dict]:
    """Job cards from a LinkedIn guest search page."""
    soup = BeautifulSoup(page_html, "html.parser")
    out = []
    for card in soup.select("[data-entity-urn*='jobPosting']") or soup.select("div.base-search-card"):
        m = re.search(r"(\d{6,})", card.get("data-entity-urn", ""))
        link = card.select_one("a.base-card__full-link") or card.select_one("a[href*='/jobs/view/']")
        href = (link.get("href") if link else "") or ""
        if not m:
            m = re.search(r"-(\d{6,})(?:\?|$)", href) or re.search(r"/view/(\d{6,})", href)
        if not m:
            continue

        def txt(sel: str) -> str:
            el = card.select_one(sel)
            return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el else ""
        out.append({"id": m.group(1),
                    "title": txt(".base-search-card__title") or txt("h3"),
                    "company": txt(".base-search-card__subtitle") or txt("h4"),
                    "location": txt(".job-search-card__location"),
                    "url": href.split("?")[0] or f"https://www.linkedin.com/jobs/view/{m.group(1)}"})
    return out


def parse_linkedin_posting(page_html: str) -> tuple[str, str]:
    """(description text, seniority level) from a LinkedIn guest job page."""
    soup = BeautifulSoup(page_html, "html.parser")
    desc_el = (soup.select_one(".show-more-less-html__markup") or soup.select_one(".description__text")
               or soup.select_one("section.description"))
    desc = html_to_text(str(desc_el)) if desc_el else ""
    if not desc:  # fallback: structured data some pages carry
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "")
            except (ValueError, TypeError):
                continue
            if isinstance(data, dict) and data.get("description"):
                desc = html_to_text(data["description"])
                break
    level = ""
    for item in soup.select(".description__job-criteria-item"):
        head = item.select_one("h3")
        if head and "seniority" in head.get_text().lower():
            val = item.select_one("span")
            level = val.get_text(strip=True) if val else ""
    return desc, level


def fetch_linkedin(role: str, company: str, loc: Optional[str], limit: int) -> list[dict]:
    """Search LinkedIn's public job pages, then read each job's description, slowly."""
    params = {"keywords": f"{role} {company}".strip(), "location": loc or "",
              "f_TPR": f"r{FRESHNESS_DAYS * 86400}", "start": 0}
    if LINKEDIN_LEVELS:
        params["f_E"] = ",".join(str(x) for x in LINKEDIN_LEVELS)
    cards, seen = [], set()
    while len(cards) < limit and params["start"] < 200:
        page = _li_get(LI_SEARCH, params)
        found = parse_linkedin_cards(page or "")
        new = [c for c in found if c["id"] not in seen]
        if not new:
            break
        for c in new:
            seen.add(c["id"])
        cards += new
        params["start"] += len(found)
    out = []
    for c in cards[:limit]:
        word = excluded_title_word(c["title"])
        if word:
            note_drop("aggregator_filter", "linkedin", c["company"], c["title"], c["location"], c["url"],
                      "excluded_title_word", f"title contains '{word}'")
            continue
        if company and not company_matches(c["company"], company):
            note_drop("aggregator_filter", "linkedin", c["company"], c["title"], c["location"], c["url"],
                      "company_mismatch", f"wanted company containing '{company}'")
            continue
        try:
            page = _li_get(LI_POSTING.format(id=c["id"]))
        except LinkedInBlocked:
            out.append(raw_record("linkedin", c["id"], c["company"], c["title"], c["location"], c["url"], ""))
            PARTIAL.extend(out)          # keep what was read before the stop
            raise
        desc, level = parse_linkedin_posting(page or "")
        if level.lower().startswith("internship"):
            note_drop("aggregator_filter", "linkedin", c["company"], c["title"], c["location"], c["url"],
                      "excluded_title_word", "LinkedIn lists it as an internship")
            continue
        out.append(raw_record("linkedin", c["id"], c["company"], c["title"], c["location"], c["url"], desc))
    return out


def test_linkedin(role: str, location: str) -> int:
    """Fetch a few LinkedIn jobs and print them. Saves nothing (your job list is not touched)."""
    log.info("Testing LinkedIn: role=%r location=%r (saves nothing)\n", role, location)
    try:
        recs = fetch_linkedin(role, "", location, 5)
    except LinkedInBlocked as e:
        log.info("LinkedIn blocked the request (%s). Try again in a few hours.", e)
        return 1
    except Exception as e:  # noqa: BLE001
        log.info("LinkedIn test failed: %s: %s", type(e).__name__, str(e)[:200])
        return 1
    if not recs:
        log.info("LinkedIn answered but no jobs could be read. Send this output to your assistant.")
        return 1
    for r in recs:
        log.info("  %-50s %-28s %5d chars of description", r["title"][:50], r["company"][:28], len(r["description"]))
    good = sum(1 for r in recs if len(r["description"]) >= MIN_DESCRIPTION_CHARS)
    log.info("\n%d of %d jobs came with a full description.", good, len(recs))
    return 0 if good else 1



def _is_rate_limited(e: Exception) -> bool:
    t = f"{type(e).__name__} {e}".lower()
    return "429" in t or "too many requests" in t or "rate limit" in t


def collect_aggregators(role: str, company: str, locations: list[str], sites: list[str],
                        country: str, limit: int, out: Optional[list] = None) -> list[dict]:
    """locations: one search per location per site (empty list = a single search with no location).
    Results are appended to `out` as they arrive, so an interrupted run still keeps them."""
    out = [] if out is None else out
    if not sites:
        return out
    try:
        from jobspy import scrape_jobs
    except ImportError:          # only Indeed/Naukri (and LinkedIn via jobspy) need it
        scrape_jobs = None
        if any(x != "linkedin" or LINKEDIN_ENGINE != "builtin" for x in sites):
            log.warning("python-jobspy not installed; Indeed/Naukri skipped (pip install python-jobspy).")
        sites = [x for x in sites if x == "linkedin" and LINKEDIN_ENGINE == "builtin"]

    query = f"{role} {company}".strip()
    for site in sites:  # one site per call so a block on one never kills the others
        searches = locations or [None]
        if site == "linkedin" and not locations and COUNTRY_FILTER:
            searches = [COUNTRY_FILTER]  # LinkedIn only filters by location; without one it searches worldwide
        linkedin_blocked = site == "linkedin" and (LI_RUN["blocked"] or _LI_STATE["used"] >= LINKEDIN_BUDGET)
        for n, loc in enumerate(searches):
            label = f"{site}/{loc}" if loc else site
            if site == "linkedin":
                if linkedin_blocked:
                    note_drop("aggregator", site, company or "", "", loc or "", "", "skipped_rate_limited",
                              "LinkedIn blocked an earlier search in this run")
                    continue
                if n and LINKEDIN_ENGINE != "builtin":
                    time.sleep(LINKEDIN_PAUSE_S)  # space LinkedIn searches out politely
            if site == "linkedin" and LINKEDIN_ENGINE == "builtin":
                try:
                    recs = fetch_linkedin(role, company, loc, min(limit, LINKEDIN_MAX))
                except LinkedInBudgetSpent as e:
                    linkedin_blocked = True
                    out += PARTIAL
                    PARTIAL.clear()
                    log.info("  [%s] LinkedIn request budget for this run is used up (%s); the rest waits for the "
                             "next run.", label, e)
                    note_drop("aggregator", site, company or "", "", loc or "", "", "linkedin_budget_spent", str(e))
                    continue
                except LinkedInBlocked as e:
                    linkedin_blocked = True
                    LI_RUN["blocked"] = True
                    out += PARTIAL
                    PARTIAL.clear()
                    log.warning("  [%s] LinkedIn is limiting requests from your connection for now (%s). "
                                "Jobs found so far are kept; try LinkedIn again in a few hours.", label, e)
                    note_drop("aggregator", site, company or "", "", loc or "", "", "rate_limited", str(e))
                    continue
                except Exception as e:  # noqa: BLE001
                    log.warning("  [%s] failed: %s", label, e)
                    note_drop("aggregator", site, company or "", "", loc or "", "", "search_error", str(e)[:200])
                    continue
                out += recs
                log.info("  [%s] %d jobs read", label, len(recs))
                continue
            try:
                kwargs = dict(
                    site_name=[site], search_term=query, location=loc,
                    results_wanted=min(limit, LINKEDIN_MAX) if site == "linkedin" else limit,
                    hours_old=24 * FRESHNESS_DAYS, country_indeed=country,
                )
                if site == "linkedin":
                    kwargs["fetch_description"] = True
                df = scrape_jobs(**kwargs)
            except Exception as e:  # noqa: BLE001
                if site == "linkedin" and _is_rate_limited(e):
                    linkedin_blocked = True
                    log.warning("  [%s] LinkedIn is limiting requests from your connection for now. "
                                "Jobs found so far are kept; try LinkedIn again in a few hours.", label)
                    note_drop("aggregator", site, company or "", "", loc or "", "", "rate_limited", str(e)[:200])
                    continue
                log.warning("  [%s] failed: %s", label, e)
                note_drop("aggregator", site, company or "", "", loc or "", "", "search_error", str(e)[:200])
                continue
            if df is None or len(df) == 0:
                log.info("  [%s] 0 results", label)
                continue
            kept = off_topic = wrong_company = excluded = 0
            for row in df.to_dict("records"):
                def s_(key: str) -> str:
                    v = row.get(key)
                    return v if isinstance(v, str) else ""
                comp, title = s_("company"), s_("title")
                url = s_("job_url_direct") or s_("job_url")
                if company and not company_matches(comp, company):
                    wrong_company += 1
                    note_drop("aggregator_filter", site, comp, title, s_("location"), url,
                              "company_mismatch", f"wanted company containing '{company}'")
                    continue
                # In broad mode, trust the job board's own relevance ranking (it searched the full text).
                if TITLE_MODE == "strict" and not title_matches(title, role):
                    off_topic += 1
                    note_drop("aggregator_filter", site, comp, title, s_("location"), url,
                              "title_mismatch", f"title lacks the words of '{role}'")
                    continue
                word = excluded_title_word(title)
                if word:
                    excluded += 1
                    note_drop("aggregator_filter", site, comp, title, s_("location"), url,
                              "excluded_title_word", f"title contains '{word}'")
                    continue
                desc = html_to_text(s_("description")).replace("\\*", "*").replace("**", "")
                out.append(raw_record(site, s_("id") or url, comp or "Unknown", title, s_("location"), url, desc))
                kept += 1
            log.info("  [%s] %d kept, %d off-topic titles, %d excluded titles, %d other companies",
                     label, kept, off_topic, excluded, wrong_company)
    return out


# --------------------------------------------------------------------------- #
# Normalisation + dedup + persistence
# --------------------------------------------------------------------------- #


def _naukri_description(url: str, rec: dict) -> bool:
    """Naukri's own job-detail feed (what its website calls). Best-effort: it may demand a captcha."""
    m = re.search(r"-(\d{9,})(?:\?|$)", url)
    if not m:
        rec["note"] = "naukri: could not find job id in link"
        return False
    headers = {"appid": "121", "systemid": "Naukri", "clientid": "d3skt0p", "Accept": "application/json",
               "Referer": url}
    r = SESSION.get(f"https://www.naukri.com/jobapi/v4/job/{m.group(1)}", headers=headers,
                    params={"microsite": "y", "src": "jobsearchDesk"}, timeout=20)
    if r.status_code != 200:
        rec["note"] = f"naukri job feed returned HTTP {r.status_code}" + (" (captcha/bot check)" if r.status_code in (403, 406) else "")
        return False
    desc = ((r.json().get("jobDetails") or {}).get("description")) or ""
    text = html_to_text(desc)
    if len(text) >= MIN_DESCRIPTION_CHARS:
        rec["description"] = text
        return True
    rec["note"] = "naukri job feed answered but had no description"
    return False


def enrich_description(rec: dict) -> bool:
    """Best-effort: if a listing came back without a usable description, fetch it from the
    board. Returns True if filled; otherwise leaves the reason in rec['note'] for the drop log."""
    if len(rec["description"].strip()) >= MIN_DESCRIPTION_CHARS or not rec["url"] or BeautifulSoup is None:
        return False
    try:
        if "naukri.com" in rec["url"] and _naukri_description(rec["url"], rec):
            return True
        r = SESSION.get(rec["url"], headers={"Accept": "text/html"}, timeout=20)
        if r.status_code != 200:
            rec.setdefault("note", f"job page returned HTTP {r.status_code}")
            return False
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "")
            except (ValueError, TypeError):
                continue
            for item in (data if isinstance(data, list) else [data]):
                if isinstance(item, dict) and item.get("@type") == "JobPosting" and item.get("description"):
                    text = html_to_text(item["description"])
                    if len(text) >= MIN_DESCRIPTION_CHARS:
                        rec["description"] = text
                        return True
        rec.setdefault("note", "job page loaded but contained no readable description")
    except Exception as e:  # noqa: BLE001
        rec.setdefault("note", f"could not read job page: {str(e)[:100]}")
    return False


def canonical_url(url: str) -> str:
    u = urlparse(url)
    return f"{u.netloc.lower()}{u.path.rstrip('/')}"


_COMPANY_NOISE = re.compile(r"\b(private|pvt|limited|ltd|inc|llc|llp|plc|gmbh|corp|corporation|co|company|"
                            r"india|technologies|technology|solutions|services|group)\b\.?")


def company_title_key(company: str, title: str) -> str:
    """'Collins Aerospace (India) Pvt Ltd' + 'Design Engineer II' -> 'collins aerospace|design engineer ii'."""
    c = _COMPANY_NOISE.sub(" ", re.sub(r"[^a-z0-9 ]", " ", (company or "").lower()))
    t = re.sub(r"[^a-z0-9 ]", " ", (title or "").lower())
    return f"{' '.join(c.split())}|{' '.join(t.split())}"


def normalize(rec: dict) -> Optional[NormalizedJob]:
    desc = rec["description"].strip()
    if len(desc) < MIN_DESCRIPTION_CHARS:
        if rec["url"] and rec["title"]:
            LEADS.append({"source": rec["source"], "company": rec["company"], "title": rec["title"],
                          "location": rec["location"], "url": rec["url"], "found": datetime.now().strftime("%Y-%m-%d"),
                          "why": rec.get("note", "no description")})
        note_drop("normalize", rec["source"], rec["company"], rec["title"], rec["location"], rec["url"],
                  "description_missing_or_too_short",
                  f"{len(desc)} characters (minimum {MIN_DESCRIPTION_CHARS}). Recovery attempt: "
                  f"{rec.get('note', 'not attempted')}")
        return None
    if not rec["url"] or not rec["title"]:
        note_drop("normalize", rec["source"], rec["company"], rec["title"], rec["location"], rec["url"],
                  "missing_url_or_title", "")
        return None
    basis = normalize_for_hash(desc) or rec["url"]
    chash = sha256(basis)
    try:
        return NormalizedJob(
            job_id=f"{rec['source']}:{rec['native_id']}",
            company=rec["company"].strip() or "Unknown",
            title=rec["title"],
            location=rec["location"] or "Not specified",
            application_url=rec["url"],
            raw_description=desc,
            content_hash=chash,
        )
    except ValidationError as e:
        log.warning("schema validation failed for %s: %s", rec.get("url"), e)
        note_drop("normalize", rec["source"], rec["company"], rec["title"], rec["location"], rec["url"],
                  "schema_validation_failed", str(e)[:300])
        return None


def save(jobs: list[NormalizedJob]) -> None:
    """Overwrite pending_jobs.json: each run replaces the previous batch."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps([j.model_dump() for j in jobs], indent=2, ensure_ascii=False))
    os.replace(tmp, OUTPUT_FILE)


def save_dropped() -> dict:
    """Write every rejected job, with its reason, to data/dropped_jobs.json."""
    summary: dict[str, int] = {}
    for d in DROPPED:
        key = f"{d['stage']}:{d['reason']}"
        summary[key] = summary.get(key, 0) + 1
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Most interesting first: jobs whose TITLE matched but something else failed come before
    # obvious off-topic titles, so possibly-relevant rejects are at the top of the file.
    order = {"description_missing_or_too_short": 0, "location_mismatch": 1, "detail_fetch_failed": 2,
             "missing_url_or_title": 3, "schema_validation_failed": 3, "beyond_max_per_source": 4,
             "company_mismatch": 5, "duplicate": 6, "excluded_title_word": 7, "title_mismatch": 8}
    ranked = sorted(DROPPED, key=lambda d: order.get(d["reason"], 5))
    payload = {"summary": dict(sorted(summary.items(), key=lambda kv: -kv[1])), "dropped": ranked}
    tmp = DROPPED_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    os.replace(tmp, DROPPED_FILE)
    return payload["summary"]


def dedupe(new_records: list[dict]) -> tuple[list[NormalizedJob], int, int, int]:
    """Validate + dedupe within this run only (the output file is replaced each run).
    Duplicates = same description, same link, or same company + title. Of two copies, the one
    with the longer description is kept (ATS records come first, so they win ties)."""
    by_hash: dict[str, int] = {}
    by_url: dict[str, int] = {}
    by_key: dict[str, int] = {}
    merged: list[Optional[NormalizedJob]] = []
    sources: list[str] = []
    dupes = dropped = 0
    for rec in new_records:
        job = normalize(rec)
        if job is None:
            dropped += 1
            continue
        cu = canonical_url(job.application_url)
        key = company_title_key(job.company, job.title)
        hit, how = None, ""
        if job.content_hash in by_hash:
            hit, how = by_hash[job.content_hash], "same description"
        elif cu in by_url:
            hit, how = by_url[cu], "same link"
        elif key in by_key:
            hit, how = by_key[key], "same company and title"
        if hit is not None and merged[hit] is not None:
            dupes += 1
            old = merged[hit]
            if len(job.raw_description) > len(old.raw_description) + 200:   # the new copy is clearly richer
                note_drop("dedupe", sources[hit], old.company, old.title, old.location, old.application_url,
                          "duplicate", f"{how} as {job.job_id}, which has the fuller description")
                merged[hit], sources[hit] = job, rec["source"]
                by_hash[job.content_hash], by_url[cu], by_key[key] = hit, hit, hit
            else:
                note_drop("dedupe", rec["source"], job.company, job.title, job.location, job.application_url,
                          "duplicate", f"{how} as already-kept job {old.job_id}")
            continue
        idx = len(merged)
        merged.append(job)
        sources.append(rec["source"])
        by_hash[job.content_hash], by_url[cu], by_key[key] = idx, idx, idx
    kept = [j for j in merged if j is not None]
    return kept, len(kept), dupes, dropped


# --------------------------------------------------------------------------- #
# Run state, leads, top-up mode, already-applied jobs
# --------------------------------------------------------------------------- #

LEADS: list[dict] = []


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except (ValueError, OSError):
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def pick_linkedin_watch(watch: list[dict], state: dict, n: int) -> list[str]:
    """The n watched companies LinkedIn covered longest ago (never-covered first), so all get a turn."""
    last = state.get("linkedin_watch_last", {})
    ranked = sorted(watch, key=lambda w: (last.get(w["name"], ""), [x["name"] for x in watch].index(w["name"])))
    return [w["name"] for w in ranked[:max(0, n)]]


def done_job_keys() -> set[str]:
    """job ids, links and content hashes of jobs you already applied to / archived (from the ledger)."""
    keys: set[str] = set()
    if not LEDGER_FILE.exists():
        return keys
    import csv
    with open(LEDGER_FILE, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("status") or "").strip().lower() in DONE_STATUSES:
                keys |= {k for k in (row.get("job_id"), row.get("content_hash"),
                                     canonical_url(row.get("application_url") or "")) if k}
    return keys


def existing_records() -> list[dict]:
    """The current pending jobs as raw records, so a top-up run keeps them (they win duplicate checks)."""
    out = []
    for j in _read_json(OUTPUT_FILE, []):
        src, _, native = str(j.get("job_id", "")).partition(":")
        out.append(raw_record(src or "earlier", native or j.get("application_url", ""), j.get("company", ""),
                              j.get("title", ""), j.get("location", ""), j.get("application_url", ""),
                              j.get("raw_description", "")))
    return out


def save_leads(jobs: list[NormalizedJob], top_up: bool) -> int:
    """Jobs without a usable description go to leads.json; a lead disappears once it becomes a real job."""
    have = {canonical_url(j.application_url) for j in jobs}
    old = _read_json(LEADS_FILE, []) if top_up else []
    merged, seen = [], set()
    for lead in [*old, *LEADS]:
        cu = canonical_url(lead.get("url", ""))
        if not cu or cu in seen or cu in have:
            continue
        seen.add(cu)
        merged.append(lead)
    _write_json(LEADS_FILE, merged)
    return len(merged)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def ask(prompt: str, default: str = "") -> str:
    try:
        return input(prompt).strip() or default
    except EOFError:
        return default


def split_locations(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"[;,|/]", text or "") if x.strip()]


def prompt_inputs(args: argparse.Namespace) -> dict:
    """Ask only for what's needed, and only search what the answers justify."""
    role = (args.role or "").strip()
    while not role:
        role = ask("Target Role (required): ")
    location = (args.location.strip() if args.location is not None
                else ask("Location(s) - separate several with commas, e.g. Hyderabad, Bengaluru (Enter to skip): "))
    locations = split_locations(location)
    company = args.company.strip() if args.company is not None else ask("Target Company (Enter to skip): ")

    if args.titles is not None:
        titles = args.titles
    else:
        print("\nHow should job titles be matched?")
        print("  1 = broad  - keep any job whose title has at least one of your role words (recommended)")
        print("  2 = strict - keep only titles with most of your role words")
        while True:
            pick = ask("Choose 1 or 2 [1]: ", "1").strip().lower()
            if pick in {"1", "broad"}:
                titles = "broad"; break
            if pick in {"2", "strict"}:
                titles = "strict"; break
            print("  Please type 1 or 2.")

    # Country to apply in: drives the Indeed country site, whether Naukri is used,
    # and (when no location is given) which portal jobs are kept.
    if args.country is not None:
        country_in = args.country.strip()
    else:
        guess = next((c for c in (infer_country(l, None) for l in locations) if c), None) or "India"
        country_in = ask(f"Country to apply in [{guess}] (type 'worldwide' for no limit): ", guess)
    worldwide = country_in.lower() in {"worldwide", "any", "global", "all"}
    country = None if worldwide else country_in

    # With no company named, sweeping every registered corporate portal is wasted
    # work unless the user explicitly wants it.
    scan_all_ats = False
    if not company and not args.no_ats:
        scan_all_ats = args.scan_all_ats or ask(
            "Also scan all registered corporate career portals? [y/N]: ").lower().startswith("y")

    if args.sites is not None:
        sites = [] if args.sites.lower() == "none" else [s.strip().lower() for s in args.sites.split(",") if s.strip()]
    else:
        sites = default_sites(country)
    return dict(role=role, company=company, location=", ".join(locations), locations=locations,
                titles=titles, country=country, indeed_country=("worldwide" if worldwide else country_in),
                sites=sites, scan_all_ats=scan_all_ats)


def check_registry() -> int:
    """Ping every registered career portal and report which ones work."""
    reg = load_registry()
    print(f"Checking {len(reg)} career portals (this takes a minute)...\n")

    def probe(item):
        name, t = item
        try:
            recs = ATS_FETCHERS[t["ats"]](t, name, "", "", 1)
            return name, t["ats"], ("OK" if recs else "OK-but-no-jobs-returned"), ""
        except Exception as e:  # noqa: BLE001
            return name, t["ats"], "FAILED", str(e)[:90]

    with ThreadPoolExecutor(max_workers=6) as ex:
        rows = list(ex.map(probe, sorted(reg.items())))
    for name, ats, status, err in rows:
        print(f"{status:<24} {name:<28} [{ats}] {err}")
    good = sum(1 for r in rows if r[2].startswith("OK"))
    print(f"\n{good} working, {len(rows) - good} failed. Remove or fix the FAILED ones in data/ats_registry.json.")
    return 0


def load_title_exclusions(profile: Optional[str]) -> list[str]:
    """exclude_title_words from the profile pack's domain.toml (empty list if none)."""
    path = profile_dir(profile) / "domain.toml"
    if not path.exists():
        return []
    try:
        with open(path, "rb") as f:
            return [w for w in tomllib.load(f).get("exclude_title_words", []) if str(w).strip()]
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"{path} has a typing mistake: {e}")


def load_watch_list(profile: Optional[str]) -> list[dict]:
    """[[watch_companies]] from the profile pack's domain.toml."""
    path = profile_dir(profile) / "domain.toml"
    if not path.exists():
        from modules.companies import watch_entries
        return watch_entries(profile)
    with open(path, "rb") as f:
        items = [w for w in tomllib.load(f).get("watch_companies", []) if w.get("name")]
    try:   # plus companies you added on the Companies page that need job-board searching
        from modules.companies import watch_entries
        have = {w["name"].lower() for w in items}
        items += [w for w in watch_entries(profile) if w["name"].lower() not in have]
    except Exception as e:  # noqa: BLE001
        log.debug("companies.json not read: %s", e)
    return items


def main() -> int:
    global COUNTRY_FILTER, FRESHNESS_DAYS, TITLE_MODE, EXCLUDE_TITLE_RE, PROFILE
    ap = argparse.ArgumentParser(description="Phase 1 job scraper")
    ap.add_argument("--role", help='one or more roles, separated by ";"  e.g. "Design Engineer; Stress Engineer"')
    ap.add_argument("--company")
    ap.add_argument("--location")
    ap.add_argument("--sites", default=None,
                    help="aggregators: indeed,naukri,linkedin,glassdoor ('none' to disable). "
                         "Default: indeed (+naukri in India) + linkedin.")
    ap.add_argument("--country", help="Country to apply in (default: inferred from location, else asked)")
    ap.add_argument("--titles", choices=["broad", "strict"], default=None,
                    help="broad: any one role word in the title (default); strict: most of them")
    ap.add_argument("--days", type=int, default=None, help="how old a listing may be, in days (default 30)")
    ap.add_argument("--max", type=int, default=25, dest="limit", help="max results per source")
    ap.add_argument("--no-ats", action="store_true", help="skip direct ATS scraping")
    ap.add_argument("--no-watch", action="store_true", help="skip the company watch list for this run")
    ap.add_argument("--add", action="store_true",
                    help="top-up: add new jobs to the current list instead of replacing it")
    ap.add_argument("--profile", help="profile folder under profiles/ (for title exclusions; default: config.toml)")
    ap.add_argument("--check-registry", action="store_true",
                    help="test every registered career portal and exit")
    ap.add_argument("--scan-all-ats", action="store_true",
                    help="with no company given, sweep every registered career portal")
    ap.add_argument("--test-portal", metavar="NAME",
                    help="fetch 5 jobs from one registered portal (e.g. wipro) for --role/--location; saves nothing")
    ap.add_argument("--test-linkedin", action="store_true",
                    help="fetch 5 LinkedIn jobs for --role/--location and print them; saves nothing")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(message)s", stream=sys.stderr)
    for noisy in ("httpx", "urllib3", "JobSpy"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    PROFILE = args.profile
    if args.check_registry:
        return check_registry()
    if args.test_portal:
        reg = load_registry()
        t = reg.get(args.test_portal.lower())
        if not t:
            log.info("'%s' is not in the registry. Known: %s", args.test_portal, ", ".join(sorted(reg)))
            return 1
        try:
            recs = ATS_FETCHERS[t["ats"]](t, args.test_portal, args.role or "Design Engineer", args.location or "Bengaluru", 5)
        except Exception as e:  # noqa: BLE001
            log.info("Portal test failed: %s: %s", type(e).__name__, str(e)[:300])
            return 1
        for feed, n in SF_FEED_COUNTS.items():
            log.info("  feed '%s' listed %d job(s)", feed, n)
        skipped = [d for d in DROPPED if d["stage"].startswith("portal")]
        if skipped:
            log.info("\n  Skipped by our filters (%d):", len(skipped))
            for d in skipped[:25]:
                log.info("    - %-55s %-18s %s", d["title"][:55], d["reason"], d["location"][:30])
        log.info("\n  Kept:")
        for r in recs:
            log.info("  %-55s %-25s %5d chars", r["title"][:55], r["location"][:25], len(r["description"]))
        good = sum(1 for r in recs if len(r["description"]) >= MIN_DESCRIPTION_CHARS)
        log.info("\n%d of %d jobs came with a full description.%s", good, len(recs),
                 "" if recs else " (No jobs matched; try another --role, or send this output to your assistant.)")
        return 0 if good else 1
    if args.test_linkedin:
        words = load_title_exclusions(args.profile)
        if words:
            EXCLUDE_TITLE_RE = re.compile(r"(?<![a-z])(" + "|".join(re.escape(w.lower()) for w in words) + r")(?![a-z])", re.I)
        return test_linkedin(args.role or "Design Engineer", args.location or "Bengaluru")

    if args.days:
        FRESHNESS_DAYS = args.days
    words = load_title_exclusions(args.profile)
    if words:
        EXCLUDE_TITLE_RE = re.compile(r"(?<![a-z])(" + "|".join(re.escape(w.lower()) for w in words) + r")(?![a-z])",
                                      re.I)

    q = prompt_inputs(args)
    roles = [r.strip() for r in re.split(r"[;|]", q["role"]) if r.strip()]   # several roles: "Design Engineer; Stress Engineer"
    company, location = q["company"], q["location"]
    COUNTRY_FILTER = q["country"]
    TITLE_MODE = q["titles"]

    log.info("\nSearching: role(s)=%s company=%r location(s)=%r country=%s, listings up to %d days old%s",
             " | ".join(roles), company or "any", location or "any", q["country"] or "worldwide", FRESHNESS_DAYS,
             "  [top-up: adding to the current list]" if args.add else "")
    log.info("Excluding titles with: %s\n", ", ".join(words) if words else "(nothing)")

    watch = load_watch_list(args.profile) if not args.no_watch else []
    for w in watch:
        ALIASES[w["name"]] = list(w.get("aliases", []))
    state = _read_json(STATE_FILE, {})
    li_watch = pick_linkedin_watch(watch, state, LINKEDIN_WATCH_PER_RUN) if "linkedin" in q["sites"] else []
    if watch:
        log.info("Watch list: %s (Indeed/Naukri every run; LinkedIn this run: %s)\n",
                 ", ".join(w["name"] for w in watch), ", ".join(li_watch) or "none")

    records: list[dict] = existing_records() if args.add else []
    n_existing = len(records)
    li_covered: list[str] = []
    try:
        for role in roles:
            if len(roles) > 1:
                log.info("=== Role: %s ===", role)
            # ATS first so corporate-portal copies win over aggregator duplicates.
            if not args.no_ats and (company or q["scan_all_ats"]):
                log.info("Direct ATS targeting...")
                records += collect_ats(role, company, location, args.limit)
            if q["sites"]:
                log.info("Aggregators (%s)...", ", ".join(q["sites"]))
                collect_aggregators(role, company, q["locations"], q["sites"], q["indeed_country"], args.limit, records)
                if not company:
                    for w in watch:
                        sites = [x for x in q["sites"] if x != "linkedin" or w["name"] in li_watch]
                        log.info("Watch list: %s (%s) ...", w["name"], ", ".join(sites))
                        before = _LI_STATE["used"]
                        collect_aggregators(role, w["name"], q["locations"], sites, q["indeed_country"],
                                            WATCH_MAX, records)
                        if "linkedin" in sites and _LI_STATE["used"] > before and not LI_RUN["blocked"]:
                            li_covered.append(w["name"])
    except KeyboardInterrupt:
        log.warning("\nStopped by you. Saving the %d listing(s) collected so far...", len(records))

    if li_covered:   # remember who LinkedIn covered, so the next run gives the others a turn
        stamp = datetime.now().isoformat(timespec="seconds")
        state.setdefault("linkedin_watch_last", {}).update({n: stamp for n in dict.fromkeys(li_covered)})
    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    _write_json(STATE_FILE, state)

    # Jobs you already applied to or archived are not brought back
    done = done_job_keys()
    if done:
        fresh = []
        for r in records:
            chash = sha256(normalize_for_hash(r["description"])) if r["description"].strip() else ""
            if f"{r['source']}:{r['native_id']}" in done or chash in done or canonical_url(r["url"]) in done:
                note_drop("ledger", r["source"], r["company"], r["title"], r["location"], r["url"],
                          "already_in_ledger", "you applied to or archived this job")
            else:
                fresh.append(r)
        records = fresh

    # Title exclusions for portals that do not filter by title themselves (Greenhouse, Lever, ...)
    kept_records = []
    for r in records:
        word = excluded_title_word(r["title"])
        if word:
            note_drop("title_filter", r["source"], r["company"], r["title"], r["location"], r["url"],
                      "excluded_title_word", f"title contains '{word}'")
        else:
            kept_records.append(r)
    records = kept_records

    thin = [r for r in records if len(r["description"].strip()) < MIN_DESCRIPTION_CHARS]
    if thin:
        log.info("\n%d listing(s) came back without a description; reading them from the job page...", len(thin))
        with ThreadPoolExecutor(max_workers=4) as ex:
            filled = sum(ex.map(enrich_description, thin))
        log.info("  recovered %d of %d", filled, len(thin))

    jobs, kept, dupes, dropped = dedupe(records)
    save(jobs)  # top-up: existing + new; otherwise replaces the previous batch
    summary = save_dropped()
    n_leads = save_leads(jobs, args.add)
    new_jobs = max(0, kept - (n_existing if args.add else 0))
    _write_json(LAST_RUN_FILE, {"finished": datetime.now().isoformat(timespec="seconds"), "roles": roles,
                                "locations": q["locations"], "top_up": bool(args.add), "jobs_total": kept,
                                "jobs_new": new_jobs, "duplicates": dupes, "leads": n_leads,
                                "linkedin_requests": _LI_STATE["used"], "linkedin_blocked": LI_RUN["blocked"],
                                "linkedin_watch": li_covered})

    by_source: dict[str, int] = {}
    for j in jobs:
        src = j.job_id.split(":", 1)[0]
        by_source[src] = by_source.get(src, 0) + 1

    log.info("\nRaw records fetched : %d", len(records))
    log.info("Jobs saved          : %d  (%s)", kept, ", ".join(f"{k} {v}" for k, v in sorted(by_source.items())))
    log.info("Duplicates dropped  : %d", dupes)
    log.info("Unusable dropped    : %d  (no link, or description under %d characters)", dropped, MIN_DESCRIPTION_CHARS)
    if args.add:
        log.info("New jobs added      : %d  (top-up; %d were already in the list)", new_jobs, n_existing)
    else:
        log.info("Previous batch cleared.")
    log.info("Leads without description: %d  (paste their text in later) -> %s", n_leads, LEADS_FILE)
    log.info("LinkedIn requests used : %d of %d%s", _LI_STATE["used"], LINKEDIN_BUDGET,
             "  (LinkedIn blocked us this run)" if LI_RUN["blocked"] else "")
    log.info("Output -> %s", OUTPUT_FILE)
    log.info("\nEverything rejected, and why -> %s", DROPPED_FILE)
    for reason, n in list(summary.items())[:12]:
        log.info("  %5d  %s", n, reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
