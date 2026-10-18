"""
Companies you add yourself (from the control centre's Companies page)
=====================================================================

Stored in profiles/<name>/companies.json:
  [{"name": "Siemens", "aliases": ["Siemens Energy", ...], "watch": true,
    "links": [{"url": "...", "platform": "workday", "supported": true, "portal": "<registry value or null>",
               "added": "...", "test": {"at": "...", "jobs": 6, "message": "..."}}]}]

For each careers link the job platform is detected:
  * supported platforms (Workday, Greenhouse, Lever, Ashby, SmartRecruiters, SAP SuccessFactors) become direct portals
    that the scraper reads like your other registered portals;
  * anything else is recorded by name (Avature, Oracle, Taleo, ...) and the company is searched on the job boards
    (LinkedIn, Indeed, Naukri) through the watch list instead.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

from modules.settings import profile_dir

SUPPORTED = {"workday", "greenhouse", "lever", "ashby", "smartrecruiters", "successfactors"}
PLATFORM_NAMES = {"workday": "Workday", "greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby",
                  "smartrecruiters": "SmartRecruiters", "successfactors": "SAP SuccessFactors"}

# Links inside a careers page that point to a platform we can read directly
ATS_LINK = re.compile(
    r"https?://(?:[\w-]+\.)*(?:myworkdayjobs\.com|myworkdaysite\.com)/[^\s\"'<>]+"
    r"|https?://(?:boards|job-boards|boards\.eu)\.greenhouse\.io/[\w-]+"
    r"|https?://jobs\.(?:eu\.)?lever\.co/[\w.-]+"
    r"|https?://jobs\.ashbyhq\.com/[\w.-]+"
    r"|https?://(?:jobs|careers)\.smartrecruiters\.com/[\w.-]+", re.I)

# Fingerprints of platforms we cannot read yet (recorded so we know which adapter is worth building)
UNSUPPORTED = [("Avature", r"avature\.net|avature"), ("Oracle Recruiting", r"oraclecloud\.com/hcmUI|fa\.\w+\.oraclecloud"),
               ("Taleo", r"taleo\.net"), ("iCIMS", r"icims\.com"), ("Eightfold", r"eightfold\.ai"),
               ("Phenom", r"phenompeople|cdn\.phenom"), ("Darwinbox", r"darwinbox"), ("Zoho Recruit", r"zohorecruit|/recruit/viewCareer"),
               ("Keka", r"keka\.com"), ("Workable", r"apply\.workable\.com"), ("Jobvite", r"jobvite\.com"),
               ("BambooHR", r"bamboohr\.com"), ("Freshteam", r"freshteam\.com"), ("SAP SuccessFactors (classic)", r"sapsf\.(com|eu)|career\d+\.successfactors")]


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #

def store_path(profile: Optional[str] = None) -> Path:
    return profile_dir(profile) / "companies.json"


def load(profile: Optional[str] = None) -> list[dict]:
    p = store_path(profile)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    except (ValueError, OSError):
        return []


def save(items: list[dict], profile: Optional[str] = None) -> None:
    p = store_path(profile)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def find(items: list[dict], name: str) -> Optional[dict]:
    return next((c for c in items if c["name"].strip().lower() == name.strip().lower()), None)


def registry_entries(profile: Optional[str] = None) -> dict[str, str]:
    """Supported links as registry entries for the scraper: {"siemens": url, "siemens (2)": url, ...}."""
    out: dict[str, str] = {}
    for c in load(profile):
        portals = [l["portal"] for l in c.get("links", []) if l.get("supported") and l.get("portal")]
        for i, url in enumerate(portals):
            out[c["name"].lower() if i == 0 else f"{c['name'].lower()} ({i + 1})"] = url
    return out


def watch_entries(profile: Optional[str] = None) -> list[dict]:
    """Companies to search on the job boards: no supported link, or 'also search job boards' ticked."""
    out = []
    for c in load(profile):
        has_portal = any(l.get("supported") for l in c.get("links", []))
        if c.get("watch", True) or not has_portal:
            out.append({"name": c["name"], "aliases": list(c.get("aliases", []))})
    return out


# --------------------------------------------------------------------------- #
# Platform detection
# --------------------------------------------------------------------------- #

def detect(url: str, fetch=None) -> dict:
    """Which job platform is behind a careers link? Returns
    {"platform", "name", "supported", "portal" (registry value or None), "note"}."""
    from modules.scraper import ats_from_url   # same rules the scraper uses
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    t = ats_from_url(url)
    if t:
        return _result(t["ats"], url, "The link points straight to the job platform.")
    fetch = fetch or _fetch
    try:
        final_url, html = fetch(url)
    except Exception as e:  # noqa: BLE001
        return {"platform": "unknown", "name": "Unknown", "supported": False, "portal": None,
                "note": f"The page could not be opened ({str(e)[:120]}). Check the link, or add it to the watch list."}
    # SAP SuccessFactors career sites (Wipro-style) advertise an RSS job feed on their own domain
    if re.search(r"rmkcdn\.successfactors|/services/rss/job", html, re.I):
        u = urlparse(final_url)
        return _result("successfactors", f"successfactors:{u.scheme}://{u.netloc}",
                       "This careers site runs on SAP SuccessFactors; its public job feed will be read.")
    for m in ATS_LINK.finditer(html):
        t = ats_from_url(m.group(0))
        if t:
            return _result(t["ats"], m.group(0), "The careers page links to its job platform.")
    for name, pattern in UNSUPPORTED:
        if re.search(pattern, html + " " + final_url, re.I):
            return {"platform": name.lower(), "name": name, "supported": False, "portal": None,
                    "note": f"This company uses {name}, which the pipeline cannot read yet. "
                            "It will be searched on LinkedIn, Indeed and Naukri instead."}
    return {"platform": "unknown", "name": "Unknown", "supported": False, "portal": None,
            "note": "No known job platform was found on that page. Try pasting the link of one job posting "
                    "from it; meanwhile the company is searched on the job boards."}


def _result(ats: str, portal: str, note: str) -> dict:
    if ats == "successfactors" and not portal.startswith("successfactors:"):
        u = urlparse(portal)
        portal = f"successfactors:{u.scheme}://{u.netloc}"
    return {"platform": ats, "name": PLATFORM_NAMES.get(ats, ats), "supported": ats in SUPPORTED,
            "portal": portal, "note": note}


def _fetch(url: str) -> tuple[str, str]:
    import requests
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/124.0 Safari/537.36")
    r = requests.get(url, headers={"User-Agent": ua, "Accept": "text/html"}, timeout=25, allow_redirects=True)
    r.raise_for_status()
    html = r.text
    # Many careers pages are a short shell: also look inside frames and "search jobs" style links one level down
    extra = []
    for m in re.finditer(r'<(?:iframe|frame)[^>]+src=["\']([^"\']+)', html, re.I):
        extra.append(urljoin(r.url, m.group(1)))
    for chunk in extra[:2]:
        try:
            html += requests.get(chunk, headers={"User-Agent": ua}, timeout=15).text
        except Exception:  # noqa: BLE001
            pass
    return r.url, html


# --------------------------------------------------------------------------- #
# Test a supported link with your current search
# --------------------------------------------------------------------------- #

def test_portal(name: str, portal: str, role: str, location: str, country: str = "India") -> dict:
    from modules import scraper as sc
    t = sc.ats_from_url(portal)
    if not t:
        return {"ok": False, "jobs": 0, "message": "Not a supported portal link."}
    sc.COUNTRY_FILTER = country or None
    sc.TITLE_MODE = "broad"
    sc.DROPPED.clear()
    try:
        recs = sc.ATS_FETCHERS[t["ats"]](t, name, role, location, 15)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "jobs": 0, "message": f"The portal did not answer: {type(e).__name__}: {str(e)[:160]}"}
    seen = len(recs) + len(sc.DROPPED)
    good = sum(1 for r in recs if len(r.get("description", "")) >= 80)
    where = location or country or "anywhere"
    msg = (f"Connected. {len(recs)} job(s) for “{role}” in {where}" +
           (f" ({good} with full descriptions)" if recs else "") +
           (f"; {seen - len(recs)} other listing(s) skipped by your filters." if seen > len(recs) else "."))
    if not recs and not seen:
        msg = f"Connected, but no “{role}” jobs right now. That is normal for some companies; it is checked every run."
    return {"ok": True, "jobs": len(recs), "message": msg}


def stamp() -> str:
    return datetime.now().isoformat(timespec="seconds")
