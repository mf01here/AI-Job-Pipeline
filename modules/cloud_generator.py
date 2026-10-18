#!/usr/bin/env python3
"""
Phase 4: Constrained bullet drafting (local draft -> Python guard -> cloud polish -> Python guard)
==================================================================================================

Input : data/evaluated_jobs.json      (Phase 2/3; includes the chosen resume variant per job)
        profiles/<name>/              (profile pack)
Output: data/tailored_drafts/<company>-<title>-<hash>.json   (machine readable, read by Phase 5)
        data/tailored_drafts/<company>-<title>-<hash>.md     (preview you can read)

How it stays honest
  1. Python builds the resume plan: for the job's resume variant it picks, role by role, exactly the bullets
     resume_profile.json allows (lead_with / then, within min/max), and moves job-relevant ones forward.
  2. Only the job-relevant bullets are rewritten; every other bullet is used word for word.
  3. The local model (LM Studio) rewrites them. It may only use the job's allowed_keywords.
  4. Python guard, per bullet. A rewrite is rejected if it
       - contains a number that is not in the original bullet,
       - contains a capitalised term / tool name / acronym that is not in the original or the allowed keywords
         (the first word too, when it looks like a tool or acronym),
       - drops an acronym or technical term from the original,
       - contains a job keyword the Fact Bank does not support, or a forbidden term from domain.toml,
       - drops most of the original meaning or grows far longer.
     Rejected bullets are retried with the exact problem; if still wrong, the ORIGINAL text is kept.
  5. A cloud model (providers in config.toml, first that works) only POLISHES wording. It sees the bullets,
     the job title/company and the allowed keywords - never the full job text or anything else.
  6. The same guard runs again, always against the ORIGINAL Fact Bank bullet.

Usage
    python modules/cloud_generator.py --check-providers      # test every key in .env, nothing is drafted
    python modules/cloud_generator.py --limit 1              # draft the first approved job
    python modules/cloud_generator.py                        # draft all approved jobs
    python modules/cloud_generator.py --status needs_review --only "Thales"
    python modules/cloud_generator.py --compare              # same job through every provider, side by side
    python modules/cloud_generator.py --local-only           # skip the cloud polish
    python modules/cloud_generator.py --fresh                # redo drafts that already exist
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.local_analyzer import (APPROVED, extract_json, has_term,  # noqa: E402
                                    is_connection_error, make_llm, norm)
from modules.profile_pack import Profile, Target, load_profile, target_view  # noqa: E402
from modules.settings import CFG, DATA_DIR, profile_dir  # noqa: E402
from schemas.models import EvaluatedJob, Phase4Draft, TailoredBullet  # noqa: E402

log = logging.getLogger("phase4")

# --------------------------------------------------------------------------- #
# Settings (edit config.toml, not this file)
# --------------------------------------------------------------------------- #
_D = CFG["drafting"]
DRAFTS_DIR = DATA_DIR / "tailored_drafts"
MAX_TAILORED = int(_D["max_tailored_bullets"])
LOCAL_ATTEMPTS = int(_D["local_attempts"])
CLOUD_ATTEMPTS = int(_D["cloud_attempts"])
MIN_RETENTION = float(_D["min_retention"])
MAX_GROWTH = float(_D["max_growth"])
STRONG = 1.5   # relevance at or above this = the bullet's text was quoted as evidence for a requirement
MAX_TOTAL = int(_D.get("max_total_bullets", 26))     # whole resume; about two pages
TITLE_BOOST = 1.5                                     # per distinctive job-title word a bullet mentions
# Words that limit scope. If the original has one, the rewrite must keep it (no quiet inflation).
QUALIFIERS = ["when consulted", "basic", "supported", "assisted", "contributed", "helped", "part of",
              "familiar", "working knowledge", "novice", "exposure", "involved in", "participated", "co-",
              "jointly", "under guidance", "partially", "some", "occasional"]
_TITLE_STOP = set("engineer engineering senior junior lead principal staff manager specialist associate design "
                  "designer analyst consultant sr jr ii iii iv the and for with of in at to".split())

STOP = set("with and the for from that this have has was were been into over under using used use via per "
           "across within their its our out all any more most than then also each both such other".split())
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#&./'-]*")
NUM = re.compile(r"\d+(?:[.,]\d+)*")
# Common irregular past-tense verbs a rewrite may start with (regular ones end in -ed and are allowed anyway)
IRREGULAR_VERBS = set("built led drove ran won set cut grew taught made wrote oversaw undertook brought began "
                      "chose kept held met put sent spent took gave got found understood saw sought thought "
                      "bought rebuilt rewrote redrew drew stood".split())


# --------------------------------------------------------------------------- #
# Providers (listed in config.toml; keys in .env)
# --------------------------------------------------------------------------- #

def provider_table() -> list[dict]:
    return [p for p in CFG["cloud"].get("providers", []) if p.get("name") and p.get("enabled", True)]


def configured_providers() -> list[str]:
    return [p["name"] for p in provider_table() if os.getenv(p.get("key_env", ""), "").strip()]


def provider_settings(name: str) -> tuple[str, str, str]:
    p = next(x for x in provider_table() if x["name"] == name)
    up = name.upper()
    return (os.getenv(p["key_env"], "").strip(),
            os.getenv(f"{up}_MODEL", "").strip() or p.get("model", ""),
            os.getenv(f"{up}_BASE_URL", "").strip() or p["base_url"])


def make_cloud(name: str) -> Callable[[list[dict]], str]:
    from openai import OpenAI
    key, model, url = provider_settings(name)
    client = OpenAI(base_url=url, api_key=key, timeout=90, max_retries=1)
    temperature = float(CFG["cloud"].get("temperature", 0.3))

    def call(messages: list[dict]) -> str:
        for attempt in (1, 2):
            try:
                r = client.chat.completions.create(model=model, messages=messages, temperature=temperature,
                                                   max_tokens=4096)
                return r.choices[0].message.content or ""
            except Exception as e:  # noqa: BLE001
                if attempt == 1 and "RateLimit" in type(e).__name__:  # free tiers allow few requests per second
                    time.sleep(8)
                    continue
                raise
        return ""
    return call


def check_providers() -> int:
    table = provider_table()
    names = configured_providers()
    if not names:
        print("No cloud keys found in .env. Add at least one of: " + ", ".join(p["key_env"] for p in table))
        return 1
    from openai import OpenAI
    print("Testing each cloud provider with one tiny request (your keys are never printed):\n")
    for name in names:
        key, model, url = provider_settings(name)
        try:
            out = make_cloud(name)([{"role": "user", "content": "Reply with the single word OK."}])
            print(f"  OK    {name:10s} model={model}   reply: {out.strip()[:30]!r}")
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL  {name:10s} model={model}   {type(e).__name__}: {str(e)[:450]}")
            try:
                ids = sorted(m.id for m in OpenAI(base_url=url, api_key=key, timeout=30).models.list().data)
                skip = re.compile(r"tts|audio|image|embed|whisper|guard|orpheus|aqa|moderation|ocr|computer-use|"
                                  r"native|veo|imagen|robotics|live|transcribe|safeguard|antigravity|deep-research|fim", re.I)
                shown = [i.replace("models/", "") for i in ids if not skip.search(i)]
                print(f"        text models this key can use (put one in config.toml, or {name.upper()}_MODEL "
                      f"in .env):\n        " + "\n        ".join(shown[:50]))
            except Exception:  # noqa: BLE001
                pass
    missing = [p["name"] for p in table if p["name"] not in names]
    if missing:
        print("\nNo key set for: " + ", ".join(missing) + " (fine, they are simply skipped)")
    return 0


# --------------------------------------------------------------------------- #
# Step 1: the resume plan (pure Python)
# --------------------------------------------------------------------------- #

def kw(s: str) -> set[str]:
    return {w[:6] for w in re.findall(r"[a-z0-9]{4,}", norm(s)) if w not in STOP}


def relevance(view, profile: Profile, keys: list[str], job_title: str = "") -> tuple[dict[str, float], dict[str, list[str]]]:
    """How strongly each bullet supports this job's met/partial requirements (and its title's key words)."""
    reqs = [r for r in view.requirements_evaluation if r.status in ("met", "partial")]
    score: dict[str, float] = {}
    why: dict[str, list[str]] = {}
    title_words = {w[:6] for w in re.findall(r"[a-z]{4,}", norm(job_title)) if w not in _TITLE_STOP}
    for key in keys:
        hits = title_words & kw(profile.line(key).text)
        if hits:
            score[key] = score.get(key, 0) + TITLE_BOOST * len(hits)
            why.setdefault(key, []).append(f"job title: {job_title}")
    for key in keys:
        text = profile.line(key).text
        bn, bk = norm(text), kw(text)
        for r in reqs:
            quotes = [norm(x) for x in [r.verbatim_quote or "", *(getattr(r, "extra_quotes", None) or [])] if x]
            weight = 1.0 if r.status == "met" else 0.5
            if any(q in bn or bn in q for q in quotes):
                score[key] = score.get(key, 0) + 3 * weight
                why.setdefault(key, []).append(r.requirement_text)
            elif bk & kw(r.requirement_text):
                score[key] = score.get(key, 0) + 0.5 * weight
                why.setdefault(key, []).append(r.requirement_text)
    return score, why


def plan_resume(ev: EvaluatedJob, profile: Profile) -> tuple[Target, list[dict], dict[str, float], dict]:
    """Which bullets go on this job's resume, role by role, in order. Returns (variant, sections, score, why)."""
    target = profile.targets.get(ev.target_id or "") or profile.default_target()
    view = ev.scored_analysis or target_view(ev.analysis, profile, target)
    candidates = [k for k in target.pool if profile.line(k) and profile.line(k).tailorable]
    score, why = relevance(view, profile, candidates, ev.job.title)
    sections = []
    for rid in target.role_order:
        sel = target.selection.get(rid)
        if not sel:
            continue
        lead = [k for k in sel.get("lead_with", []) if profile.line(k)]
        then = [k for k in sel.get("then", []) if profile.line(k)]
        strong = sorted([k for k in lead + then if score.get(k, 0) >= STRONG], key=lambda k: -score[k])
        rest_lead = [k for k in lead if k not in strong]
        rel_then = sorted([k for k in then if k not in strong and score.get(k, 0) > 0], key=lambda k: -score[k])
        other = [k for k in then if k not in strong and k not in rel_then]
        ordered = strong + rest_lead + rel_then + other
        lo = int(sel.get("min_bullets", 0))
        hi = int(sel.get("max_bullets", len(ordered)))
        n = min(len(ordered), max(lo, min(hi, len(strong) + len(rest_lead) + len(rel_then))))
        if n:
            sections.append({"role_id": rid, "keys": ordered[:n]})
    # Whole-resume budget (about two pages): drop the least relevant optional bullets, never below a role's minimum.
    mins = {rid: int(target.selection.get(rid, {}).get("min_bullets", 0)) for rid in target.role_order}
    leads = {k for rid in target.role_order for k in target.selection.get(rid, {}).get("lead_with", [])}
    while sum(len(sec["keys"]) for sec in sections) > MAX_TOTAL:
        options = [(score.get(k, 0), k in leads, -i, sec, k) for sec in sections if len(sec["keys"]) > mins.get(sec["role_id"], 0)
                   for i, k in enumerate(sec["keys"])]
        if not options:
            break
        _, _, _, sec, k = min(options, key=lambda o: (o[0], o[1], o[2]))
        sec["keys"].remove(k)
    sections = [sec for sec in sections if sec["keys"]]
    extra = [k for k in target.include_additional if profile.line(k)]
    if extra:
        sections.append({"role_id": "additional", "keys": extra})
    return target, sections, score, why


# --------------------------------------------------------------------------- #
# The guard (pure Python)
# --------------------------------------------------------------------------- #

def stem(w: str) -> str:
    w = w.lower()
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def numbers(s: str) -> set[str]:
    return {m.replace(",", "") for m in NUM.findall(s)}


def _looks_special(core: str) -> bool:
    return core[0].isupper() or any(c.isdigit() for c in core) or sum(c.isupper() for c in core) >= 2


def _first_word_new_terms(core: str, orig_tokens: set[str], allowed_tokens: set[str]) -> list[str]:
    """The first word is capitalised anyway, so only flag it when it looks like a tool, acronym or new name."""
    known = orig_tokens | allowed_tokens
    parts = [p for p in re.split(r"[-/]", core) if p]
    bad = []
    for i, p in enumerate(parts):
        if stem(p) in known:
            continue
        acronym_like = any(c.isdigit() for c in p) or sum(c.isupper() for c in p) >= 2
        if acronym_like or (i > 0 and p[0].isupper()):
            bad.append(p)
        elif i == 0 and len(parts) > 1 and p[0].isupper():          # "Ansys-validated", "CATIA-modelled"
            bad.append(p)
        elif i == 0 and len(parts) == 1 and not (p.lower().endswith(("ed", "ing")) or p.lower() in IRREGULAR_VERBS):
            bad.append(p)                                           # "Ansys validated ..." / "Teamcenter ..."
    return bad


PAST_IRREGULAR = IRREGULAR_VERBS


def _is_past(word: str) -> bool:
    w = word.lower().strip(".,;:")
    return w.endswith("ed") or w in PAST_IRREGULAR


def check_bullet(original: str, new: str, allowed: list[str], forbidden: list[str], tense: str = "") -> list[str]:
    """Return a list of problems; an empty list means the rewrite is acceptable."""
    new = (new or "").strip()
    if not new:
        return ["empty text"]
    problems = []
    on_, nn_ = norm(original), norm(new)
    lost = [q for q in QUALIFIERS if has_term(q, on_) and not has_term(q, nn_)]
    if lost:
        problems.append(f"drops words that limit the scope of the claim: {lost}")
    first_new = (WORD.findall(new) or [""])[0]
    first_old = (WORD.findall(original) or [""])[0]
    if tense == "present" and _is_past(first_new) and not _is_past(first_old):
        problems.append("switches a current role to past tense; keep present tense (e.g. 'Design', 'Manage')")
    if tense == "past" and first_new and not _is_past(first_new) and _is_past(first_old):
        problems.append("switches a past role out of past tense")
    extra = numbers(new) - numbers(original)
    if extra:
        problems.append(f"contains numbers that are not in the original: {sorted(extra)}")

    orig_tokens = {stem(t.strip(".'-/")) for t in WORD.findall(original)}
    orig_tokens |= {stem(p) for t in WORD.findall(original) for p in re.split(r"[-/]", t.strip(".'-/")) if p}
    allowed_tokens = {stem(w.strip(".'-/")) for k in allowed for w in WORD.findall(k)}
    new_terms = []
    for i, t in enumerate(WORD.findall(new)):
        core = t.strip(".'-/")
        if not core:
            continue
        if i == 0:
            new_terms += _first_word_new_terms(core, orig_tokens, allowed_tokens)
        elif _looks_special(core) and stem(core) not in orig_tokens and stem(core) not in allowed_tokens:
            new_terms.append(core)
    if new_terms:
        problems.append(f"contains terms that are not in the original or the allowed keywords: {sorted(set(new_terms))}")

    # acronyms, tools and capitalised technical terms in the original must survive (ATS keywords, and truthfulness)
    new_stems = {stem(t.strip(".'-/")) for t in WORD.findall(new)}
    new_stems |= {stem(p) for t in WORD.findall(new) for p in re.split(r"[-/]", t.strip(".'-/")) if p}
    dropped = []
    for i, t in enumerate(WORD.findall(original)):
        core = t.strip(".'-/")
        if not core or i == 0:
            continue
        if (core[0].isupper() or sum(c.isupper() for c in core) >= 2) and stem(core) not in new_stems:
            dropped.append(core)
    if dropped:
        problems.append(f"drops terms that must be kept exactly: {sorted(set(dropped))}")

    on, nn = norm(original), norm(new)
    for term in forbidden:
        nt = norm(term)
        if nt and has_term(nt, nn) and not has_term(nt, on):
            problems.append(f'uses "{term}", which the candidate has no evidence for')

    ok = kw(original)
    if ok and len(ok & kw(new)) / len(ok) < MIN_RETENTION:
        problems.append("drops too much of the original meaning")
    if len(new) > MAX_GROWTH * len(original) + 30:
        problems.append("is much longer than the original")
    return problems


# --------------------------------------------------------------------------- #
# Model prompts
# --------------------------------------------------------------------------- #

DRAFT_SYSTEM = """You rewrite a candidate's resume bullets for ONE specific job.
Output ONLY one JSON object, no markdown, no commentary:
{"tailored_bullets":[{"original_id":"<id>","rewritten_text":"<one line>","justification":"<short>"}]}

Strict rules:
1. Keep every fact. Do NOT add, change or remove any number, percentage, tool, software, employer, scope or result.
2. Do NOT invent skills. You may use the ALLOWED KEYWORDS only where the bullet truthfully describes that skill,
   in place of a synonym. If none fits naturally, do not use any.
3. Use the TENSE given for each bullet (present for a current role, past for earlier roles). Keep it one line,
   plain text, no bullet symbol, similar length.
4. Mirror the job's wording only where it truthfully means the same thing as the original.
5. Keep every acronym, tool, standard and technical term exactly as written (for example ECRs, ECNs, DMU, EAPS). Never drop one.
6. Keep the meaning of the action verb and the person's role (designed, owned, technical lead, presented and defended).
   Do not swap it for a weaker or different verb such as coordinated, administered or oversaw. If unsure, keep the original verb.
7. Never remove words that limit a claim (when consulted, basic, supported, assisted, contributed, part of).
   Keep singular and plural exactly as written (one installation stays one installation).
   Keep the original spelling (British English: modelling, standardised) and use plain hyphens (-).
8. Return one entry for EVERY bullet given, using the exact original_id."""

POLISH_SYSTEM = """You are an ATS-focused resume editor. You receive resume bullets that are already tailored to a job.
Polish the wording only: clearer, tighter, strong action verbs, natural flow. Output ONLY one JSON object:
{"tailored_bullets":[{"original_id":"<id>","rewritten_text":"<one line>","justification":"<short>"}]}

Strict rules:
1. Do NOT add any fact, number, tool, software, employer, scope or result that is not in the bullet.
2. Do NOT add keywords. Keep the allowed keywords already used if they are there.
3. One line per bullet, plain text, no bullet symbol, similar length. Use the TENSE given for each bullet.
4. Keep every acronym, tool, standard and technical term exactly as written (for example ECRs, ECNs, DMU, EAPS). Never drop one.
5. Keep the meaning of the action verb and the person's role (designed, owned, technical lead, presented and defended).
   Do not swap it for a weaker or different verb such as coordinated, administered or oversaw. If unsure, keep the original verb.
6. Never remove words that limit a claim (when consulted, basic, supported, assisted, contributed, part of).
   Keep singular and plural exactly as written. Keep the original British spelling and plain hyphens (-).
7. Return one entry for EVERY bullet, with the exact original_id."""


def bullets_block(items: list[tuple]) -> str:
    out = []
    for item in items:
        bid, text, reqs = item[0], item[1], item[2]
        line = f'- id: {bid}\n  bullet: {text}'
        if len(item) > 3 and item[3]:
            line += f"\n  tense: {item[3]}"
        if reqs:
            line += "\n  helps with: " + "; ".join(r[:80] for r in reqs)
        out.append(line)
    return "\n".join(out)


PAIR_RE = re.compile(r'"(?:original_id|id)"\s*:\s*"([^"]+)"\s*,\s*"(?:rewritten_text|text|rewritten)"\s*:\s*"((?:[^"\\]|\\.)*)"', re.S)


_TYPO = str.maketrans({"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u00a0": " ", "\u202f": " ", "\u2009": " "})


def clean_text(text: str, original: str = "") -> str:
    """Plain ASCII hyphens and spaces (ATS keyword matching breaks on look-alike characters), and the
    original's spelling kept: 'modeling' becomes 'modelling' when the original says 'modelling'."""
    t = re.sub(r"\s+", " ", (text or "").translate(_TYPO)).strip()
    if original:
        orig_words = {w.lower(): w for w in re.findall(r"[A-Za-z]+", original.translate(_TYPO))}

        def fix(m: re.Match) -> str:
            w = m.group(0)
            lw = w.lower()
            if lw in orig_words:
                return w
            for cand in (lw.replace("z", "s"), re.sub(r"([aeiou])l(ing|ed|er)$", r"\1ll\2", lw),
                         lw.replace("center", "centre"), lw.replace("color", "colour")):
                if cand != lw and cand in orig_words:
                    return orig_words[cand] if w[0].islower() == orig_words[cand][0].islower() else cand.capitalize()
            return w
        t = re.sub(r"[A-Za-z]+", fix, t)
    return t


def recover_pairs(raw: str, valid_ids: set[str]) -> dict[str, str]:
    """Last resort for replies whose JSON is slightly broken: pull out each id/text pair directly."""
    out = {}
    for bid, text in PAIR_RE.findall(re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S)):
        try:
            text = json.loads(f'"{text}"')
        except ValueError:
            pass
        text = re.sub(r"^[-•*]\s*", "", re.sub(r"\s+", " ", text).strip())
        if bid.strip() in valid_ids and text:
            out[bid.strip()] = text
    return out


def parse_bullets(raw: str, valid_ids: set[str]) -> dict[str, str]:
    """Read the model's reply, tolerating common variations in key names and ids and slightly broken JSON."""
    try:
        got = _parse_strict(raw, valid_ids)
    except ValueError:
        got = {}
    return got or recover_pairs(raw, valid_ids)


def _parse_strict(raw: str, valid_ids: set[str]) -> dict[str, str]:
    data = extract_json(raw)
    rows = None
    if isinstance(data, dict):
        for key in ("tailored_bullets", "bullets", "rewritten_bullets", "results", "items", "output"):
            if isinstance(data.get(key), list):
                rows = data[key]
                break
        if rows is None:  # {"acd_001": "rewritten text", ...}
            rows = [{"original_id": k, "rewritten_text": v} for k, v in data.items() if isinstance(v, str)]
    out = {}
    for b in rows or []:
        if not isinstance(b, dict):
            continue
        bid = next((str(b[k]).strip() for k in ("original_id", "id", "bullet_id", "source_id") if b.get(k)), "")
        text = next((str(b[k]) for k in ("rewritten_text", "rewritten", "text", "bullet", "tailored_text", "new_text") if b.get(k)), "")
        bid = bid.removeprefix("id:").strip().strip('"\'')
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r"^[-•*]\s*", "", text)
        if bid in valid_ids and text:
            out[bid] = text
    return out


def run_stage(call: Callable[[list[dict]], str], system: str, header: str, items: list[tuple[str, str, list[str]]],
              originals: dict[str, str], allowed: list[str], forbidden: list[str],
              attempts: int) -> tuple[dict[str, str], list[str]]:
    """Ask the model to rewrite `items`; keep only guard-approved rewrites. Returns (accepted, notes)."""
    accepted: dict[str, str] = {}
    notes: list[str] = []
    pending = list(items)
    feedback = ""
    for attempt in range(1, attempts + 1):
        if not pending:
            break
        user = f"{header}\n\nBULLETS:\n{bullets_block(pending)}{feedback}"
        raw = call([{"role": "system", "content": system}, {"role": "user", "content": user}])
        got = parse_bullets(raw, {i[0] for i in pending})
        if not got:
            notes.append(f"    nothing usable in the reply; it began: {raw.strip()[:240]!r}")
        failed = []
        problems_text = []
        for item in pending:
            bid = item[0]
            new = got.get(bid)
            if new is not None:
                new = clean_text(new, originals[bid])
            if new is None:
                failed.append(item)
                problems_text.append(f'- id {bid}: missing from your reply')
                continue
            problems = check_bullet(originals[bid], new, allowed, forbidden, item[3] if len(item) > 3 else "")
            if problems:
                failed.append(item)
                problems_text.append(f'- id {bid}: your text "{new}" {"; ".join(problems)}')
            else:
                accepted[bid] = new
        pending = failed
        if pending:
            feedback = ("\n\nYour previous reply had problems. Fix ONLY these bullets and return them again, "
                        "keeping every fact exactly:\n" + "\n".join(problems_text))
            notes.append(f"attempt {attempt}: {len(pending)} bullet(s) rejected by the guard")
            for line in problems_text[:3]:   # show WHY, so a bad prompt or a too-strict rule is easy to spot
                notes.append("    " + line[:260])
    return accepted, notes


# --------------------------------------------------------------------------- #
# One job
# --------------------------------------------------------------------------- #

def job_context(ev: EvaluatedJob, allowed: list[str]) -> str:
    return (f"JOB TITLE: {ev.job.title}\nCOMPANY: {ev.job.company}\n"
            f"ALLOWED KEYWORDS (use only if truthful): {', '.join(allowed) if allowed else '(none)'}")


def _tense(profile: Profile, key: str) -> str:
    ln = profile.line(key)
    end = str(profile.role(ln.role_id).get("end", "")).strip().lower() if ln and ln.role_id else ""
    return "present" if end in ("present", "current", "now", "ongoing") else ("past" if ln and ln.role_id else "")


def draft_job(ev: EvaluatedJob, profile: Profile, local: Callable, providers: list[str]) -> dict:
    if ev.analysis is None:
        raise ValueError("this job has no analysis (run Phase 2 first)")
    target, sections, score, why = plan_resume(ev, profile)
    planned = [k for s in sections for k in s["keys"]]
    if not planned:
        raise ValueError("the resume plan is empty; check resume_profile.json")
    originals = {k: profile.line(k).text for k in planned}
    to_tailor = sorted([k for k in planned if score.get(k, 0) > 0], key=lambda k: -score[k])[:MAX_TAILORED]
    view = ev.scored_analysis or ev.analysis
    allowed = list(view.allowed_keywords)
    allowed_n = {norm(a) for a in allowed}
    forbidden = [k for k in view.missing_keywords if norm(k) not in allowed_n] + list(profile.forbidden_terms)
    header = job_context(ev, allowed)
    notes: list[str] = []
    current = dict(originals)
    source = {k: "original" for k in planned}

    if not allowed:     # nothing verified to add: your original wording is used, no cosmetic rewording
        to_tailor = []
    if to_tailor:
        # local draft + guard
        tense = {k: _tense(profile, k) for k in to_tailor}
        items = [(k, originals[k], why.get(k, [])[:3], tense[k]) for k in to_tailor]
        local_ok, n = run_stage(local, DRAFT_SYSTEM, header, items, originals, allowed, forbidden, LOCAL_ATTEMPTS)
        notes += [f"local {x}" for x in n]
        for k, text in local_ok.items():
            if text != originals[k]:
                current[k], source[k] = text, "local"

        # cloud polish + guard (always judged against the ORIGINAL Fact Bank text)
        provider_used = None
        for name in providers:
            try:
                polish_items = [(k, current[k], [], tense[k]) for k in to_tailor]
                ok, n = run_stage(make_cloud(name), POLISH_SYSTEM, header, polish_items, originals, allowed,
                                  forbidden, CLOUD_ATTEMPTS)
                notes += [f"{name} {x}" for x in n]
                if ok:
                    for k, text in ok.items():
                        if text != current[k]:
                            current[k], source[k] = text, "cloud"
                    provider_used = name
                    break
                notes.append(f"{name}: no polished bullet passed the guard, trying next provider")
            except Exception as e:  # noqa: BLE001
                notes.append(f"{name} failed: {type(e).__name__}: {str(e)[:120]}")
        if providers and provider_used is None:
            notes.append("cloud polish unavailable; using the local draft")
        # keep a rewrite only if it really works in a verified job keyword; otherwise the original wording
        reverted = 0
        for k in to_tailor:
            if source[k] != "original" and not any(
                    has_term(norm(a), norm(current[k])) and not has_term(norm(a), norm(originals[k])) for a in allowed):
                current[k], source[k] = originals[k], "original"
                reverted += 1
        if reverted:
            notes.append(f"{reverted} rewrite(s) added no job keyword, so your original wording was kept")
    else:
        provider_used = None
        notes.append("no verified job keyword to add; your bullets are used word for word "
                     "(tailoring = choice and order of bullets)")

    def row(k: str) -> dict:
        ln = profile.line(k)
        used = [a for a in allowed if has_term(norm(a), norm(current[k])) and not has_term(norm(a), norm(originals[k]))]
        return {"id": k, "role_id": ln.role_id or "additional", "project": ln.project, "original": originals[k],
                "final": current[k], "source": source[k], "relevance": round(score.get(k, 0), 2),
                "supports": list(dict.fromkeys(why.get(k, [])))[:5],   # job requirements this bullet proves
                "integrated_keywords": used}

    out_sections = []
    for s in sections:
        meta = profile.role(s["role_id"])
        out_sections.append({"role_id": s["role_id"], "employer": meta.get("employer", ""),
                             "title": meta.get("title", ""), "start": meta.get("start", ""),
                             "end": meta.get("end", ""), "bullets": [row(k) for k in s["keys"]]})
    # Evidence map for later features (cover letter, outreach message, interview prep): what the job asks,
    # how it was judged, and which Fact Bank lines prove it. Everything here is already verified.
    evidence = [{"requirement": r.requirement_text, "type": r.type, "status": r.status,
                 "quotes": [q for q in [r.verbatim_quote, *(getattr(r, "extra_quotes", None) or [])] if q]}
                for r in view.requirements_evaluation]
    return {"target": target, "sections": out_sections, "provider": provider_used, "notes": notes,
            "local_model": getattr(local, "model_name", ""), "evidence": evidence,
            "missing_keywords": list(view.missing_keywords)}


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #

def slug(ev: EvaluatedJob) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", f"{ev.job.company}-{ev.job.title}".lower()).strip("-")[:60]
    return f"{base}-{ev.job.content_hash[:8]}"


def save_draft(ev: EvaluatedJob, result: dict, fb_hash: str) -> Path:
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    rows = [r for s in result["sections"] for r in s["bullets"]]
    draft = Phase4Draft(
        tailored_bullets=[TailoredBullet(original_id=r["id"], rewritten_text=r["final"],
                                         justification=("unchanged from the Fact Bank" if r["source"] == "original" else
                                                        f'rewritten by {r["source"]} model; keywords added: '
                                                        f'{", ".join(r["integrated_keywords"]) or "none"}'))
                          for r in rows],
        integrated_keywords=sorted({k for r in rows for k in r["integrated_keywords"]}))
    target = result["target"]
    payload = {"job": {"job_id": ev.job.job_id, "company": ev.job.company, "title": ev.job.title,
                       "location": ev.job.location, "application_url": ev.job.application_url,
                       "content_hash": ev.job.content_hash},
               "score": ev.overall_score, "status": ev.status, "fact_bank_hash": fb_hash,
               "target": {"id": target.target_id, "label": target.label},
               "generated_at": datetime.now().isoformat(timespec="seconds"),
               "local_model": result["local_model"], "polish_provider": result["provider"],
               "notes": result["notes"], "sections": result["sections"], "draft": draft.model_dump(),
               "evidence": result.get("evidence", []), "missing_keywords": result.get("missing_keywords", []),
               "status_reasons": list(ev.status_reasons)}
    path = DRAFTS_DIR / f"{slug(ev)}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    md = [f"# {ev.job.title} @ {ev.job.company}", "",
          f"- Score: {ev.overall_score:.0f}%  |  Resume variant: {target.label}",
          f"- Apply: {ev.job.application_url}",
          f"- Polish provider: {result['provider'] or 'none (local only)'}", ""]
    for s in result["sections"]:
        head = "Additional" if s["role_id"] == "additional" else \
            f"{s['employer']} - {s['title']} ({s['start']} to {s['end']})"
        md += [f"## {head}", ""]
        for r in s["bullets"]:
            if r["source"] == "original":
                md += [f"- {r['final']}"]
            else:
                md += ["", f"- **Tailored ({r['source']}):** {r['final']}", f"  - *Original:* {r['original']}"]
                if r["integrated_keywords"]:
                    md += [f"  - *Keywords worked in:* {', '.join(r['integrated_keywords'])}"]
                md += [""]
        md += [""]
    if result["notes"]:
        md += ["## Notes", *[f"- {n}" for n in result["notes"]]]
    path.with_suffix(".md").write_text("\n".join(md), encoding="utf-8")
    return path


def select_jobs(args) -> list[EvaluatedJob]:
    p = DATA_DIR / "evaluated_jobs.json"
    if not p.exists():
        raise SystemExit(f"{p} not found. Run Phase 2/3 first: python modules/local_analyzer.py")
    jobs = [EvaluatedJob(**r) for r in json.loads(p.read_text(encoding="utf-8"))]
    if getattr(args, "job_id", None):
        return [j for j in jobs if j.job.job_id in set(args.job_id) and j.analysis is not None]
    if args.status != "all":
        jobs = [j for j in jobs if j.status == args.status]
    for term in args.only or []:
        t = term.lower()
        jobs = [j for j in jobs if t in j.job.company.lower() or t in j.job.title.lower()]
    return sorted(jobs, key=lambda j: -j.overall_score)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 4: constrained bullet drafting")
    ap.add_argument("--profile", help="profile folder name under profiles/ (default: config.toml)")
    ap.add_argument("--model", default="", help="local model name (default: the model loaded in LM Studio)")
    ap.add_argument("--status", default=APPROVED, help="which jobs to draft: approved_for_phase4 (default), needs_review, all")
    ap.add_argument("--only", action="append", metavar="TEXT", help="only jobs whose company/title contains TEXT (repeatable)")
    ap.add_argument("--limit", type=int, help="draft at most this many jobs")
    ap.add_argument("--job-id", action="append", metavar="ID", help="draft this job whatever its status (repeatable)")
    ap.add_argument("--fresh", action="store_true", help="redo drafts that already exist")
    ap.add_argument("--local-only", action="store_true", help="skip the cloud polish")
    ap.add_argument("--check-providers", action="store_true", help="test the keys in .env and exit")
    ap.add_argument("--compare", action="store_true", help="run the first selected job through every provider and print the results")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s", stream=sys.stderr)
    for noisy in ("httpx", "openai", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.check_providers:
        return check_providers()

    profile = load_profile(profile_dir(args.profile))
    fb_hash = profile.fact_hash
    providers = [] if args.local_only else configured_providers()
    jobs = select_jobs(args)
    if not jobs:
        log.info("No jobs with status '%s'%s: nothing to draft.", args.status,
                 f" matching {args.only}" if args.only else "")
        return 0
    stale = [j for j in jobs if j.fact_bank_hash and j.fact_bank_hash != fb_hash]
    if stale:
        log.warning("Note: %d job(s) were analysed with an older Fact Bank. Run Phase 2 again first for "
                    "accurate drafts: python modules/local_analyzer.py\n", len(stale))
    local = make_llm(args.model)

    if args.compare:
        ev = jobs[0]
        log.info("Comparing providers on: %s @ %s\n", ev.job.title, ev.job.company)
        for name in providers or ["(no cloud keys found)"]:
            res = draft_job(ev, profile, local, [name] if providers else [])
            log.info("=== %s ===", name)
            for s in res["sections"]:
                for r in s["bullets"]:
                    if r["source"] != "original":
                        log.info("  [%s] %s", r["source"], r["final"])
            for n in res["notes"]:
                log.info("  note: %s", n)
            log.info("")
        return 0

    todo = [j for j in jobs if args.fresh or not (DRAFTS_DIR / f"{slug(j)}.json").exists()]
    skipped = len(jobs) - len(todo)
    if args.limit is not None:
        todo = todo[:args.limit]
    log.info("Profile: %s | jobs selected: %d | to draft now: %d (%d already drafted) | local model: %s | cloud: %s\n",
             profile.name, len(jobs), len(todo), skipped, local.model_name,
             " -> ".join(providers) if providers else "off (local only)")

    done = 0
    for i, ev in enumerate(todo, 1):
        t0 = time.time()
        log.info("[%d/%d] %s @ %s  (%.0f%%, %s)", i, len(todo), ev.job.title[:60], ev.job.company,
                 ev.overall_score, ev.target_id or "-")
        try:
            res = draft_job(ev, profile, local, providers)
            path = save_draft(ev, res, fb_hash)
            rows = [r for s in res["sections"] for r in s["bullets"]]
            counts = {s: sum(1 for r in rows if r["source"] == s) for s in ("cloud", "local", "original")}
            log.info("    -> %d bullets (cloud %d, local %d, word for word %d)  polish: %s  %.0fs\n       %s",
                     len(rows), counts["cloud"], counts["local"], counts["original"],
                     res["provider"] or "none", time.time() - t0, path.with_suffix(".md"))
            done += 1
        except Exception as e:  # noqa: BLE001
            if is_connection_error(e):
                raise SystemExit("\nCannot reach LM Studio. Load the model, start the Local Server "
                                 "(http://localhost:1234) and run this again. Finished drafts are saved.")
            log.error("    FAILED: %s\n", str(e)[:200])
    log.info("Done: %d draft(s) saved in %s", done, DRAFTS_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
