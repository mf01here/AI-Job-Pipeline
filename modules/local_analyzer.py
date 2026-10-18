#!/usr/bin/env python3
"""
Phase 2 + 3: Local Gap Analysis and Deterministic Triage Gate
=============================================================

Input : data/pending_jobs.json            (Phase 1)
        profiles/<name>/                  (profile pack: fact_bank.json, resume_profile.json, domain.toml)
Output: data/evaluated_jobs.json          (list of EvaluatedJob)

PHASE 2 - the local model (LM Studio) reads one job at a time and judges each requirement
against the Fact Bank. Python then enforces the zero-hallucination rules:
  * output must validate against Phase2GapAnalysis (retry with the error message up to 3 times)
  * "met" needs a verbatim_quote that really exists, word for word, in ONE Fact Bank line;
    otherwise it is downgraded to "partial"
  * missing_keywords that are actually in the Fact Bank are removed
  * allowed_keywords must (a) appear in the job text, (b) not be in the Fact Bank already, and
    (c) be tied to a requirement judged met/partial. Keywords from "unmet" requirements are never allowed.

PHASE 3 - pure Python, no model involved:
  The job is scored once for EACH resume variant in resume_profile.json (e.g. design / support).
  For a variant, a "met" proven only by lines that variant cannot show, or only by a limited-proficiency
  skill (novice, working knowledge), counts as partial. The best-scoring variant is kept.
  hard match rate  = (met + 0.5 x partial) / number of hard requirements
  overall score    = 70% hard + 30% preferred  (only hard if there are no preferred ones)
  hard rate < 40%                  -> archived_below_threshold
  overall >= 60%                   -> approved_for_phase4
  anything in between              -> needs_review
  Safety override: visa / work-authorisation / security-clearance / language blockers never auto-approve.
  All numbers live in config.toml.

Each run rebuilds evaluated_jobs.json from the CURRENT pending_jobs.json. Analyses are reused only when the
job text and the Fact Bank are unchanged; scoring rules and resume variants are re-applied every run.

Usage:
    python modules/local_analyzer.py                       # analyse everything not yet analysed
    python modules/local_analyzer.py --limit 3             # try just 3 new jobs first
    python modules/local_analyzer.py --reanalyze needs_review   # second pass with a different loaded model
    python modules/local_analyzer.py --fresh               # ignore all saved results
    python modules/local_analyzer.py --profile someone     # use profiles/someone/
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # `python modules/x.py` puts modules/, not the project root, on sys.path
    sys.path.insert(0, str(ROOT))

from modules.profile_pack import Profile, load_profile, target_view  # noqa: E402
from modules.settings import CFG, DATA_DIR, model_profile, profile_dir  # noqa: E402
from schemas.models import (EvaluatedJob, NormalizedJob,  # noqa: E402
                            Phase2GapAnalysis, RequirementEvidence)

log = logging.getLogger("analyzer")

# --------------------------------------------------------------------------- #
# Settings (edit config.toml, not this file)
# --------------------------------------------------------------------------- #
_G, _A = CFG["gates"], CFG["analysis"]
HARD_GATE_PCT = float(_G["hard_gate_pct"])
SCORE_GATE_PCT = float(_G["score_gate_pct"])
HARD_WEIGHT = float(_G["hard_weight"])
PREFERRED_WEIGHT = float(_G["preferred_weight"])
MIN_DESC_CHARS = int(_G["min_desc_chars"])
GROUND_MIN_SHARE = float(_G["ground_min_share"])
MIN_HARD_FOR_APPROVAL = int(_G["min_hard_for_approval"])
PARTIAL_CREDIT = float(_G["partial_credit"])
VARIANT_TIE_PCT = float(_G.get("variant_tie_pct", 3.0))
LIMITED_TOOL_TITLE_REVIEW = bool(_G.get("limited_tool_in_title_review", True))

MAX_REQUIREMENTS = int(_A["max_requirements"])
MAX_DESC_CHARS = int(_A["max_desc_chars"])
MAX_ATTEMPTS = int(_A["max_attempts"])
CONTEXT_WARN_TOKENS = int(_A["context_warn_tokens"])

APPROVED = "approved_for_phase4"
REVIEW = "needs_review"
ARCHIVED = "archived_below_threshold"
FAILED = "analysis_failed"

LM_STUDIO_BASE_URL = CFG["local_llm"]["base_url"]

# Generic defaults; a profile's domain.toml may replace them (see apply_domain).
SENSITIVE_FLAG = re.compile(
    r"visa|work permit|work authori[sz]ation|right to work|clearance|citizen|nationality|"
    r"fluen\w*|native (speaker|level)|language requirement", re.I)
SENSITIVE_TEXT = re.compile(
    r"security clearance|clearance (is )?required|must (be|hold|have) [^.]{0,40}citizen|"
    r"citizens? only|no (visa )?sponsorship|work authori[sz]ation (is )?required", re.I)
_CONCEPTS: list[tuple[str, str]] = []   # requirement pattern -> evidence pattern (from domain.toml)
TOOL_TERMS: list[str] = []               # named tools: a requirement naming one needs a quote naming one (domain.toml)


def apply_domain(profile: Profile) -> None:
    """Load the profession-specific rules of the active profile pack."""
    global SENSITIVE_FLAG, SENSITIVE_TEXT, _CONCEPTS, TOOL_TERMS
    if profile.sensitive_flag:
        SENSITIVE_FLAG = re.compile(profile.sensitive_flag, re.I)
    if profile.sensitive_text:
        SENSITIVE_TEXT = re.compile(profile.sensitive_text, re.I)
    _CONCEPTS = list(profile.concepts)
    TOOL_TERMS = list(profile.tool_terms)


SYSTEM_PROMPT = """You are a strict, literal job-requirements auditor. You compare ONE job description \
against a candidate's verified FACT BANK. You never invent, assume, or embellish experience.
Output ONLY one JSON object. No markdown, no code fences, no commentary.

Rules:
1. Extract the concrete requirements of the job (at most 12; merge near-duplicates).
   - "hard_requirement": stated as required / must have / minimum / essential, or listed as a core responsibility skill.
   - "preferred_qualification": nice to have / a plus / preferred / desirable.
2. Judge each requirement ONLY from the FACT BANK:
   - "met": the Fact Bank directly supports it. You MUST copy verbatim_quote EXACTLY, character for
     character, from a SINGLE Fact Bank line. Do not shorten with "...", do not paraphrase.
   - "partial": related or adjacent evidence exists but it does not fully satisfy the requirement.
     Explain briefly in semantic_evidence.
   - "unmet": no supporting evidence. verbatim_quote must be null.
3. If you cannot copy an exact quote, the status is NOT "met".
   extra_quotes: if OTHER Fact Bank lines (for example from a different role) also prove the requirement, copy up
   to 2 of them exactly, character for character. Otherwise leave the list empty.
4. missing_keywords: important tools/terms from the job that do NOT appear in the Fact Bank.
5. allowed_keywords: job keywords the candidate genuinely has the underlying skill for (see a "partial" or "met"
   requirement) but describes with different words. NEVER list a keyword for an "unmet" requirement.
6. risk_flags: short factual flags, e.g. visa / work authorisation, security clearance, required language,
   relocation, on-site requirement, large years-of-experience gap, seniority mismatch.
7. Keep it compact: semantic_evidence under 15 words. Inside any text value, write a double quote as \".

Example of the exact output shape:
{"requirements_evaluation":[{"requirement_text":"3+ years of Python","type":"hard_requirement","status":"met","verbatim_quote":"<text copied exactly from one Fact Bank line>","extra_quotes":["<another exact line, optional>"],"semantic_evidence":"short reason"},
{"requirement_text":"Experience with CATIA V6","type":"preferred_qualification","status":"unmet","verbatim_quote":null,"semantic_evidence":null}],
"missing_keywords":["CATIA V6"],"allowed_keywords":[],"risk_flags":["Requires security clearance"]}"""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def has_term(term: str, blob: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", blob))


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_json(text: str) -> dict:
    """Strip everything a chatty model wraps around its JSON: <think> blocks, ``` fences, prose."""
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S | re.I)
    if "</think>" in t:                      # opening tag missing
        t = t.split("</think>", 1)[1]
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S | re.I)
    if fence:
        t = fence.group(1)
    # Read the FIRST complete JSON object and ignore anything the model appends after it.
    decoder = json.JSONDecoder()
    last_err = None
    pos = t.find("{")
    if pos == -1:
        raise ValueError("no JSON object found in the model's reply")
    while pos != -1:
        try:
            obj, _ = decoder.raw_decode(t, pos)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError as e:
            last_err = e
        pos = t.find("{", pos + 1)
    raise ValueError(f"no valid JSON object in the model's reply ({last_err})")


def _loads_lenient(text: str):
    """json.loads that also tolerates trailing commas and curly quotes around keys/values."""
    t = text.replace("\u201c", '"').replace("\u201d", '"')
    t = re.sub(r",\s*([}\]])", r"\1", t)
    return json.loads(t)


def recover_analysis(text: str) -> tuple[dict, bool]:
    """Last resort when the model's overall JSON is broken or cut off: rebuild it from every complete
    requirement object we can read, plus the keyword/flag lists if present. Returns (data, recovered)."""
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S | re.I)
    decoder = json.JSONDecoder()
    reqs, seen = [], set()
    for m in re.finditer(r"\{", t):
        try:
            obj, _ = decoder.raw_decode(t, m.start())
        except json.JSONDecodeError:
            try:  # small repairs on just this object
                end = t.find("}", m.start())
                obj = _loads_lenient(t[m.start():end + 1]) if end != -1 else None
            except (ValueError, TypeError):
                obj = None
        if isinstance(obj, dict) and obj.get("requirement_text") and obj["requirement_text"] not in seen:
            seen.add(obj["requirement_text"])
            reqs.append(obj)

    def arr(key: str) -> list:
        m = re.search(rf'"{key}"\s*:\s*(\[[^\]]*\])', t, re.S)
        if not m:
            return []
        try:
            v = _loads_lenient(m.group(1))
            return [x for x in v if isinstance(x, str)]
        except (ValueError, TypeError):
            return re.findall(r'"([^"]+)"', m.group(1))
    data = {"requirements_evaluation": reqs, "missing_keywords": arr("missing_keywords"),
            "allowed_keywords": arr("allowed_keywords"), "risk_flags": arr("risk_flags")}
    return data, bool(reqs)


INCOMPLETE_FLAG = "Incomplete analysis: the model's reply was damaged or cut off"


# --------------------------------------------------------------------------- #
# Phase 2: enforcement of the zero-hallucination rules (pure Python)
# --------------------------------------------------------------------------- #

_STOP = set("with and the for from that this have has experience knowledge ability skills skill strong good work "
            "working using use used years year plus preferred required requirement understanding proficiency "
            "proficient hands familiarity such other related well least minimum including include into over".split())


def is_grounded(requirement_text: str, desc_n: str) -> bool:
    """A requirement must really come from the job text: most of its key words must appear in it."""
    words = [w for w in re.findall(r"[a-z0-9+#./-]{3,}", norm(requirement_text)) if w not in _STOP]
    if not words:
        return False
    hit = sum(1 for w in words if w in desc_n or (len(w) > 5 and w[:5] in desc_n))
    return hit / len(words) >= GROUND_MIN_SHARE


_GENERIC = set("design designs designed team lead leads leading manage managed management develop development "
               "engineer engineering engineers project projects complete system systems process activities "
               "activity requirements requirement support provide ensure perform work".split())
YEARS_RE = re.compile(r"\b\d{1,2}\s*(?:\+|plus)?\s*(?:(?:to|-|–)\s*\d{1,2}\s*)?\+?\s*years?\b", re.I)
ADVANCED_DEGREE_RE = re.compile(r"\b(master'?s?|advanced degree|post.?graduate|ph\.?\s?d|m\.?\s?tech|m\.?\s?e\b|m\.?\s?s\b|msc)\b", re.I)
BACHELOR_RE = re.compile(r"\b(bachelor'?s?|b\.?\s?tech|b\.?\s?e\b|b\.?\s?sc|be\b|undergraduate|graduate degree|diploma)", re.I)
DEGREE_RE = re.compile(r"\b(b\.?\s?tech|b\.?\s?e\b|bachelor|master|m\.?\s?tech|degree|diploma|ph\.?d|graduate)", re.I)


def quote_relevant(requirement_text: str, quote: str) -> bool:
    """The quote must share at least one specific word with the requirement it is supposed to prove."""
    q = norm(quote)
    rq = norm(requirement_text)
    if any(re.search(a, rq) and re.search(b, q) for a, b in _CONCEPTS):
        return True
    words = [w for w in re.findall(r"[a-z0-9+#./-]{3,}", norm(requirement_text))
             if w not in _STOP and w not in _GENERIC]
    if not words:
        return True  # nothing specific to compare
    return any(w in q or (len(w) > 5 and w[:5] in q) for w in words)


_MARK = "[auto-downgraded from met] "


def tighten(analysis: Phase2GapAnalysis) -> Phase2GapAnalysis:
    """Extra Python checks on 'met' verdicts. Reversible: a downgrade keeps the quote and is marked, so
    changing the rules later and re-running restores or re-applies it correctly."""
    flags, out = [f for f in analysis.risk_flags if not f.startswith(("Years of experience cannot",
                                                                       "Quote does not match", "Requires an advanced",
                                                                       "Names "))], []
    for r in analysis.requirements_evaluation:
        ev = r.semantic_evidence or ""
        if ev.startswith(_MARK):                       # undo an earlier downgrade, then judge again
            r = r.model_copy(update={"status": "met", "semantic_evidence": ev[len(_MARK):] or None})
        if r.status == "met":
            reason, new_status = None, "partial"
            req, quote = r.requirement_text or "", r.verbatim_quote or ""
            tools = [t for t in TOOL_TERMS if has_term(norm(t), norm(req))]
            if (ADVANCED_DEGREE_RE.search(req) and not BACHELOR_RE.search(req)
                    and re.search(r"bachelor", quote, re.I)):
                reason = f"Requires an advanced degree; a Bachelor's does not meet it: {req[:70]}"
                new_status = "unmet"
            elif tools and not any(has_term(norm(t), norm(quote)) for t in tools):
                reason = f"Names {', '.join(tools[:3])}, but the evidence does not mention it, downgraded to partial: {req[:60]}"
            elif YEARS_RE.search(r.requirement_text or ""):
                reason = f"Years of experience cannot be proven by a quote; check yourself: {r.requirement_text[:70]}"
            elif r.verbatim_quote and not quote_relevant(r.requirement_text, r.verbatim_quote):
                reason = f"Quote does not match the requirement, downgraded to partial: {r.requirement_text[:70]}"
            if reason:
                flags.append(reason)
                r = r.model_copy(update={"status": new_status,
                                         "semantic_evidence": _MARK + (r.semantic_evidence or "")})
        out.append(r)
    return analysis.model_copy(update={"requirements_evaluation": out,
                                       "risk_flags": list(dict.fromkeys(flags))})


def drop_ungrounded(analysis: Phase2GapAnalysis, description: str) -> Phase2GapAnalysis:
    desc_n = norm(description)
    keep, dropped = [], 0
    for r in analysis.requirements_evaluation:
        if is_grounded(r.requirement_text, desc_n):
            keep.append(r)
        else:
            dropped += 1
    flags = list(analysis.risk_flags)
    if dropped:
        flags.append(f"{dropped} requirement(s) removed: they do not appear in the job text (model invented them)")
    return analysis.model_copy(update={"requirements_evaluation": keep,
                                       "risk_flags": list(dict.fromkeys(flags))})


def enforce(analysis: Phase2GapAnalysis, lines: list[str], description: str) -> Phase2GapAnalysis:
    lines_n = [norm(x) for x in lines]
    blob = " \n ".join(lines_n)
    analysis = drop_ungrounded(analysis, description)
    flags = list(analysis.risk_flags)
    reqs, seen = [], set()

    for r in analysis.requirements_evaluation:
        key = norm(r.requirement_text)
        if not key or key in seen:
            continue
        seen.add(key)

        rtype = norm(r.type).replace(" ", "_")
        rtype = ("hard_requirement" if rtype.startswith("hard") or rtype.startswith("require")
                 else "preferred_qualification" if rtype.startswith("pref") or "nice" in rtype
                 else "")
        if not rtype:
            flags.append(f"Requirement skipped (unclear type): {r.requirement_text[:70]}")
            continue
        status = norm(r.status)
        if status not in {"met", "partial", "unmet"}:
            status = "unmet"  # conservative

        quote = (r.verbatim_quote or "").strip().strip('"“”\'')
        if quote and (len(quote) < 3 or not any(norm(quote) in ln for ln in lines_n)):
            quote = ""
            if status == "met":
                status = "partial"
                flags.append(f"Evidence could not be verified, downgraded to partial: {r.requirement_text[:70]}")
        if status == "met" and not quote:
            status = "partial"
            flags.append(f"No verbatim evidence, downgraded to partial: {r.requirement_text[:70]}")
        if status == "unmet":
            quote = ""

        extras = []
        for x in (r.extra_quotes or [])[:2]:
            xq = (x or "").strip().strip('"“”\'')
            if len(xq) >= 3 and xq != quote and any(norm(xq) in ln for ln in lines_n):
                extras.append(xq)
        if status == "unmet":
            extras = []
        reqs.append(r.model_copy(update={"type": rtype, "status": status, "verbatim_quote": quote or None,
                                         "extra_quotes": extras}))
        if len(reqs) >= MAX_REQUIREMENTS:
            break

    missing, seen_kw = [], set()
    for k in analysis.missing_keywords:
        nk = norm(k)
        if nk and nk not in seen_kw and not has_term(nk, blob):  # present in Fact Bank = not missing
            missing.append(k.strip())
        seen_kw.add(nk)

    desc_n = norm(description)
    backed = " ".join(norm(f"{r.requirement_text} {r.semantic_evidence or ''}")
                      for r in reqs if r.status in ("met", "partial"))
    allowed, seen_al = [], set()
    for k in analysis.allowed_keywords:
        nk = norm(k)
        if (nk and nk not in seen_al and has_term(nk, desc_n)      # real keyword from the job
                and not has_term(nk, blob)                         # not already in the Fact Bank
                and has_term(nk, backed)):                         # tied to a met/partial requirement
            allowed.append(k.strip())
        seen_al.add(nk)

    return tighten(Phase2GapAnalysis(requirements_evaluation=reqs, missing_keywords=missing,
                                     allowed_keywords=allowed, risk_flags=list(dict.fromkeys(flags))))


# --------------------------------------------------------------------------- #
# Phase 3: deterministic scoring and gating (pure Python)
# --------------------------------------------------------------------------- #

def _rate(rs: list[RequirementEvidence]) -> Optional[float]:
    if not rs:
        return None
    pts = sum(1.0 if r.status == "met" else PARTIAL_CREDIT if r.status == "partial" else 0.0 for r in rs)
    return 100.0 * pts / len(rs)


def score(analysis: Phase2GapAnalysis) -> tuple[Optional[float], Optional[float], float]:
    hard_reqs = [r for r in analysis.requirements_evaluation if r.type == "hard_requirement"]
    hard = _rate(hard_reqs)
    pref = _rate([r for r in analysis.requirements_evaluation if r.type == "preferred_qualification"])
    if hard is not None and pref is not None and len(hard_reqs) < MIN_HARD_FOR_APPROVAL:
        # too few hard requirements to deserve 70% of the weight: every requirement counts the same
        overall = _rate(analysis.requirements_evaluation)
    elif hard is not None and pref is not None:
        overall = HARD_WEIGHT * hard + PREFERRED_WEIGHT * pref
    else:
        overall = hard if hard is not None else (pref if pref is not None else 0.0)
    return hard, pref, overall


def decide_status(analysis: Phase2GapAnalysis, hard: Optional[float], overall: float,
                  description: str) -> tuple[str, list[str]]:
    if not analysis.requirements_evaluation:
        return REVIEW, ["No requirements could be extracted from this job description"]
    if hard is not None and hard < HARD_GATE_PCT:
        return ARCHIVED, [f"Hard-requirement match {hard:.1f}% is below the {HARD_GATE_PCT:.0f}% gate"]
    n_hard = sum(1 for r in analysis.requirements_evaluation if r.type == "hard_requirement")
    degree_gap = [r.requirement_text for r in analysis.requirements_evaluation
                  if r.type == "hard_requirement" and r.status == "unmet" and DEGREE_RE.search(r.requirement_text)]
    if overall >= SCORE_GATE_PCT and degree_gap:
        return REVIEW, [f"Score {overall:.1f}% would qualify, but a required degree is not matched: {degree_gap[0][:90]}"]
    if overall >= SCORE_GATE_PCT and n_hard < MIN_HARD_FOR_APPROVAL:
        return REVIEW, [f"Score {overall:.1f}% rests on only {n_hard} hard requirement(s) (need at least "
                        f"{MIN_HARD_FOR_APPROVAL} to auto-approve); the posting is probably too short to judge"]
    if overall >= SCORE_GATE_PCT:
        blockers = [f for f in analysis.risk_flags if SENSITIVE_FLAG.search(f)]
        text_hit = SENSITIVE_TEXT.search(description or "")
        if text_hit:
            blockers.append(f'Job text says: "{text_hit.group(0)}"')
        if blockers:
            return REVIEW, [f"Score {overall:.1f}% would qualify, but a blocker needs your check: {b}" for b in blockers]
        return APPROVED, [f"Overall {overall:.1f}% meets the {SCORE_GATE_PCT:.0f}% gate"]
    return REVIEW, [f"Overall {overall:.1f}% is between the {HARD_GATE_PCT:.0f}% hard gate and the {SCORE_GATE_PCT:.0f}% approval gate"]


def _too_short(ev: EvaluatedJob) -> EvaluatedJob:
    n = len((ev.job.raw_description or "").strip())
    return ev.model_copy(update={
        "status": REVIEW, "overall_score": 0.0, "hard_match_rate": None, "preferred_match_rate": None,
        "target_id": None, "target_scores": {}, "scored_analysis": None,
        "status_reasons": [f"Job description is only {n} characters; too short to judge. "
                           "Open the link and paste the full text later."],
        "analysis": Phase2GapAnalysis(requirements_evaluation=[], missing_keywords=[], risk_flags=[])})


def judge(ev: EvaluatedJob, profile: Profile) -> EvaluatedJob:
    """Phase 3 for one job: score every resume variant, keep the best, apply the gates. No model call."""
    desc = ev.job.raw_description or ""
    if len(desc.strip()) < MIN_DESC_CHARS:
        return _too_short(ev)
    if ev.analysis is None:
        return ev
    analysis = tighten(drop_ungrounded(ev.analysis, desc))
    best, scores, options = None, {}, []
    for t in profile.targets.values():
        view = target_view(analysis, profile, t)
        hard, pref, overall = score(view)
        scores[t.target_id] = round(overall, 1)
        rank = (round(overall, 6), t.title_match(ev.job.title))
        if best is None or rank > best[0]:        # ties keep the earlier variant
            best = (rank, t, view, hard, pref, overall)
        options.append((overall, t.title_match(ev.job.title), t, view, hard, pref))
    # near-tie (within VARIANT_TIE_PCT points): the variant whose typical job titles match this title wins
    top = best[5]
    close = [o for o in options if top - o[0] <= VARIANT_TIE_PCT]
    pick = max(close, key=lambda o: (o[1], o[0]))
    if pick[2] is not best[1]:
        best = ((round(pick[0], 6), pick[1]), pick[2], pick[3], pick[4], pick[5], pick[0])
    _, t, view, hard, pref, overall = best
    status, reasons = decide_status(view, hard, overall, desc)
    if status == APPROVED and any(f.startswith(INCOMPLETE_FLAG) for f in view.risk_flags):
        status, reasons = REVIEW, [f"Score {overall:.1f}% would qualify, but the model's reply was damaged or cut off, "
                                   "so some requirements may be missing. Please check this one yourself."]
    if status == APPROVED and LIMITED_TOOL_TITLE_REVIEW:
        hit = next((t for t in profile.limited_terms
                    if re.search(rf"(?<![a-z0-9]){re.escape(t.lower())}(?![a-z0-9])", ev.job.title.lower())), None)
        if hit:
            status, reasons = REVIEW, [f"Score {overall:.1f}% would qualify, but the job title names '{hit}', which "
                                       "you rated limited proficiency. A recruiter may screen on it: your call."]
    if len(profile.targets) > 1:
        others = ", ".join(f"{k} {v:.1f}%" for k, v in scores.items())
        reasons.append(f"Resume variant: {t.label} (scores: {others})")
    return ev.model_copy(update={"analysis": analysis, "scored_analysis": view, "target_id": t.target_id,
                                 "target_scores": scores, "status": status, "status_reasons": reasons,
                                 "overall_score": round(overall, 1),
                                 "hard_match_rate": None if hard is None else round(hard, 1),
                                 "preferred_match_rate": None if pref is None else round(pref, 1)})


# Kept for older callers: same as judge() with the profile of the current settings.
def retriage(prev: EvaluatedJob, profile: Profile | None = None) -> EvaluatedJob:
    return judge(prev, profile or load_profile(profile_dir()))


# --------------------------------------------------------------------------- #
# Model call
# --------------------------------------------------------------------------- #

def detect_loaded_model(base_url: str) -> str:
    """Name of the model currently loaded in LM Studio ('' if it cannot be told)."""
    root = base_url.rstrip("/")
    root = root[:-3] if root.endswith("/v1") else root
    try:  # LM Studio's own endpoint reports which models are actually loaded
        with urllib.request.urlopen(root + "/api/v0/models", timeout=3) as r:
            data = json.load(r).get("data", [])
        loaded = [m["id"] for m in data if m.get("state") == "loaded" and m.get("type", "llm") in ("llm", "vlm")]
        if len(loaded) == 1:
            return loaded[0]
    except Exception:  # noqa: BLE001 - not LM Studio, or not running
        pass
    try:  # any OpenAI-compatible server: only trust it when exactly one model is listed
        with urllib.request.urlopen(base_url.rstrip("/") + "/models", timeout=3) as r:
            data = json.load(r).get("data", [])
        if len(data) == 1:
            return data[0]["id"]
    except Exception:  # noqa: BLE001
        pass
    return ""


def make_llm(model: str = "") -> Callable[[list[dict]], str]:
    """A function that sends messages to the local model. call.model_name holds the model's real name.
    Every call logs prompt size, reply size and speed, so slow or cut-off replies are easy to spot."""
    from openai import OpenAI
    lc = CFG["local_llm"]
    client = OpenAI(base_url=lc["base_url"], api_key=lc["api_key"], timeout=float(lc.get("timeout_s", 900)),
                    max_retries=0)
    if model in ("", "local-model"):
        model = lc.get("model") or detect_loaded_model(lc["base_url"]) or "local-model"
    prof = model_profile(model)

    def call(messages: list[dict], temperature: Optional[float] = None) -> str:
        msgs = [dict(m) for m in messages]
        if prof.get("prompt_suffix") and msgs and msgs[0]["role"] == "system":
            msgs[0]["content"] = msgs[0]["content"].rstrip() + "\n" + prof["prompt_suffix"]
        t0 = time.time()
        kwargs = {"extra_body": prof["extra_body"]} if prof.get("extra_body") else {}
        temp = prof["temperature"] if temperature is None else temperature
        r = client.chat.completions.create(model=model, messages=msgs, temperature=temp,
                                           max_tokens=int(prof["max_tokens"]), **kwargs)
        secs = time.time() - t0
        if getattr(r, "model", None):
            call.model_name = r.model
        choice = r.choices[0]
        content = choice.message.content or ""
        extra = getattr(choice.message, "model_extra", None) or {}
        reasoning = extra.get("reasoning_content") or extra.get("reasoning") or ""
        if not content.strip() and reasoning:   # some models (e.g. Qwen 3.5) put the whole reply here
            content = reasoning
        u = getattr(r, "usage", None)
        if u is not None:
            out_tok = getattr(u, "completion_tokens", 0) or 0
            log.info("    model call: %s prompt tokens, %s reply tokens, %.0fs (about %.1f reply tokens/s)%s",
                     getattr(u, "prompt_tokens", "?"), out_tok, secs, out_tok / secs if secs else 0,
                     "; includes hidden thinking" if reasoning else "")
        cut = choice.finish_reason == "length"
        if cut:
            log.warning("    the reply hit the max_tokens limit (%s) and was cut off", prof["max_tokens"])
        call.last = {"cut_off": cut, "thinking": bool(reasoning), "seconds": secs,
                     "reply_tokens": getattr(u, "completion_tokens", 0) if u is not None else 0}
        return content

    call.model_name = model
    call.last = {}
    return call


THINKING_HELP = ("the model spent its whole reply budget 'thinking' and never wrote the answer. Retrying cannot help. "
                 "Switch thinking off for this model (see the [[local_llm.model_profiles]] entries in config.toml) "
                 "or test it first with:  python modules/local_analyzer.py --probe")


def probe(model: str = "") -> int:
    """One small request (about 30 seconds) that shows the loaded model's name, speed and whether it 'thinks'."""
    call = make_llm(model)
    print(f"Model: {call.model_name}")
    msgs = [{"role": "system", "content": "Output ONLY one JSON object, nothing else."},
            {"role": "user", "content": 'List three colours as {"colours": ["...", "...", "..."]}'}]
    try:
        reply = call(msgs)
    except Exception as e:  # noqa: BLE001
        print(f"FAILED: {type(e).__name__}: {str(e)[:300]}")
        return 1
    info = call.last
    ok = True
    try:
        extract_json(reply)
    except ValueError:
        ok = False
    print(f"Reply: {reply.strip()[:200]!r}")
    print(f"Valid JSON: {'yes' if ok else 'NO'} | thinking: {'YES - slow, switch it off' if info.get('thinking') else 'no'}"
          f" | {info.get('reply_tokens', '?')} reply tokens in {info.get('seconds', 0):.0f}s")
    return 0 if ok and not info.get("thinking") else 1


def is_connection_error(e: Exception) -> bool:
    return "connection" in type(e).__name__.lower() or "connect" in str(e).lower()[:80]


def analyze_job(job: NormalizedJob, profile: Profile, call: Callable[[list[dict]], str]) -> Phase2GapAnalysis:
    desc = job.raw_description
    if len(desc) > MAX_DESC_CHARS:
        desc = desc[:MAX_DESC_CHARS] + "\n[truncated]"
    # Everything that is the same for every job comes FIRST (Fact Bank, schema), the job comes LAST.
    # LM Studio then reuses its work on the shared start of the prompt, so each job is processed much faster.
    user = (f"=== FACT BANK ===\n{profile.prompt_text()}\n\n"
            f"Return ONLY one JSON object, matching this schema:\n"
            f"{json.dumps(Phase2GapAnalysis.model_json_schema())}\n\n"
            f"=== JOB ===\nJOB TITLE: {job.title}\nCOMPANY: {job.company}\nLOCATION: {job.location}\n\n"
            f"JOB DESCRIPTION:\n{desc}")
    approx_tokens = len(SYSTEM_PROMPT + user) // 3
    if approx_tokens > CONTEXT_WARN_TOKENS:
        log.warning("    prompt is about %d tokens; set LM Studio's Context Length to at least 16384 or the "
                    "reply may be cut off", approx_tokens)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]
    last = ""
    salvage = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        content = ""
        try:
            content = call(messages, temperature=None if attempt == 1 else 0.3)   # vary retries
            try:
                data = extract_json(content)
                if "requirements_evaluation" not in data:
                    raise ValueError("reply has no requirements_evaluation at the top level")
                parsed = Phase2GapAnalysis.model_validate(data)
                if getattr(call, "last", {}).get("cut_off"):
                    parsed = parsed.model_copy(update={"risk_flags": [*parsed.risk_flags, INCOMPLETE_FLAG]})
                return enforce(parsed, profile.quotable(), job.raw_description)
            except (ValidationError, ValueError, KeyError, TypeError):
                data, ok = recover_analysis(content)
                if ok:
                    try:
                        salvage = Phase2GapAnalysis.model_validate(
                            {**data, "risk_flags": [*data["risk_flags"], INCOMPLETE_FLAG]})
                    except ValidationError:
                        pass
                raise
        except (ValidationError, ValueError, KeyError, TypeError) as e:  # ValueError covers JSONDecodeError
            last = str(e).strip().splitlines()[0][:300] if str(e).strip() else type(e).__name__
            info = getattr(call, "last", {}) or {}
            if info.get("cut_off") and info.get("thinking"):
                raise RuntimeError(THINKING_HELP) from e
            log.warning("    attempt %d/%d: output invalid (%s)", attempt, MAX_ATTEMPTS, last)
            messages += [{"role": "assistant", "content": content[:3000] or "{}"},
                         {"role": "user", "content": f"That output was invalid: {last}\n"
                                                     "Return ONLY the corrected JSON object, with the keys "
                                                     "requirements_evaluation, missing_keywords, allowed_keywords and "
                                                     "risk_flags at the top level. Escape any double quote inside a text "
                                                     "value as \\\". Keep semantic_evidence under 15 words."}]
    if salvage is not None and salvage.requirements_evaluation:
        log.warning("    recovered %d requirement(s) from the damaged reply; this job will be held for your review",
                    len(salvage.requirements_evaluation))
        return enforce(salvage, profile.quotable(), job.raw_description)
    raise RuntimeError(f"model output failed validation {MAX_ATTEMPTS} times: {last}")


def evaluate(job: NormalizedJob, profile: Profile, fb_hash: str, call: Callable[[list[dict]], str]) -> EvaluatedJob:
    now = datetime.now().isoformat(timespec="seconds")
    if len((job.raw_description or "").strip()) < MIN_DESC_CHARS:
        return _too_short(EvaluatedJob(job=job, status=REVIEW, model_used="(skipped)",
                                       fact_bank_hash=fb_hash, analyzed_at=now))
    analysis = analyze_job(job, profile, call)
    ev = EvaluatedJob(job=job, status=REVIEW, analysis=analysis, model_used=getattr(call, "model_name", ""),
                      fact_bank_hash=fb_hash, analyzed_at=now)
    return judge(ev, profile)


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #

def load_jobs(path: Path) -> list[NormalizedJob]:
    if not path.exists():
        raise SystemExit(f"{path} not found. Run Phase 1 first: python modules/scraper.py")
    return [NormalizedJob(**r) for r in json.loads(path.read_text(encoding="utf-8"))]


def load_cache(path: Path) -> dict[str, EvaluatedJob]:
    if not path.exists():
        return {}
    try:
        return {e.job.content_hash: e for e in (EvaluatedJob(**r) for r in json.loads(path.read_text(encoding="utf-8")))}
    except Exception as e:  # noqa: BLE001
        log.warning("Saved results unreadable (%s); starting fresh.", str(e)[:80])
        return {}


def save(entries: list[EvaluatedJob], path: Path) -> None:
    order = {APPROVED: 0, REVIEW: 1, ARCHIVED: 2, FAILED: 3}
    ranked = sorted(entries, key=lambda e: (order.get(e.status, 9), -e.overall_score))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps([e.model_dump() for e in ranked], indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def summarize(entries: list[EvaluatedJob]) -> None:
    counts: dict[str, int] = {}
    for e in entries:
        counts[e.status] = counts.get(e.status, 0) + 1
    log.info("\nResults: " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())))
    top = sorted([e for e in entries if e.status != FAILED], key=lambda e: -e.overall_score)[:10]
    if top:
        log.info("\nTop matches:")
        for e in top:
            log.info("  %5.1f%%  %-24s %-28s %s @ %s", e.overall_score, e.status, (e.target_id or "-")[:28],
                     e.job.title[:48], e.job.company)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 2+3: local gap analysis and triage")
    ap.add_argument("--profile", help="profile folder name under profiles/ (default: config.toml)")
    ap.add_argument("--jobs", default=str(DATA_DIR / "pending_jobs.json"))
    ap.add_argument("--out", default=str(DATA_DIR / "evaluated_jobs.json"))
    ap.add_argument("--model", default="", help="local model name (default: the model loaded in LM Studio)")
    ap.add_argument("--limit", type=int, help="analyse at most this many NEW jobs, then stop")
    ap.add_argument("--fresh", action="store_true", help="ignore all saved results")
    ap.add_argument("--reanalyze", metavar="STATUS",
                    help="redo jobs currently in this status, e.g. needs_review (use after loading a different model)")
    ap.add_argument("--probe", action="store_true", help="quick check of the loaded model (speed, thinking), then exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s", stream=sys.stderr)
    for noisy in ("httpx", "openai", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if args.probe:
        return probe(args.model)

    profile = load_profile(profile_dir(args.profile))
    apply_domain(profile)
    fb_hash = profile.fact_hash
    jobs = load_jobs(Path(args.jobs))
    out_path = Path(args.out)
    cache = {} if args.fresh else load_cache(out_path)
    call = make_llm(args.model)

    log.info("Profile: %s | Fact Bank lines: %d | resume variants: %s", profile.name, len(profile.quotable()),
             ", ".join(profile.targets))
    log.info("Jobs: %d | saved results: %d | model: %s\n", len(jobs), len(cache), call.model_name)

    entries: list[EvaluatedJob] = []
    todo: list[NormalizedJob] = []
    for job in jobs:
        prev = cache.get(job.content_hash)
        reusable = (prev is not None and prev.status != FAILED and prev.fact_bank_hash == fb_hash
                    and prev.status != args.reanalyze)
        if reusable:
            entries.append(judge(prev, profile))
        else:
            todo.append(job)
    if args.limit is not None:
        todo = todo[:args.limit]
    log.info("%d job(s) to analyse, %d reused from the last run\n", len(todo), len(entries))
    save(entries, out_path)  # drops results for jobs that are no longer pending

    for i, job in enumerate(todo, 1):
        t0 = time.time()
        log.info("[%d/%d] %s @ %s", i, len(todo), job.title[:60], job.company)
        try:
            ev = evaluate(job, profile, fb_hash, call)
            log.info("    -> %s  (overall %.1f%%, hard %s, variant %s)  %.0fs", ev.status, ev.overall_score,
                     "n/a" if ev.hard_match_rate is None else f"{ev.hard_match_rate:.1f}%",
                     ev.target_id or "-", time.time() - t0)
        except Exception as e:  # noqa: BLE001
            if is_connection_error(e):
                save(entries, out_path)
                raise SystemExit("\nCannot reach LM Studio. Open LM Studio, load a model, start the Local Server "
                                 f"(address {LM_STUDIO_BASE_URL}), then run this again. Finished work is saved.")
            log.error("    FAILED: %s", str(e)[:200])
            ev = EvaluatedJob(job=job, status=FAILED, status_reasons=["Model analysis failed; it will be retried next run"],
                              model_used=call.model_name, fact_bank_hash=fb_hash,
                              analyzed_at=datetime.now().isoformat(timespec="seconds"), error=str(e)[:300])
        entries.append(ev)
        save(entries, out_path)  # incremental: a crash never loses finished work

    summarize(entries)
    log.info("\nSaved -> %s", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
