"""
Profile pack loader
===================

A profile pack is one folder under profiles/ that makes the generic engine specific to one person:

  profiles/<name>/fact_bank.json      every verified fact (roles, bullets, skills, education, certificates)
  profiles/<name>/resume_profile.json resume variants ("target roles"): which roles, bullets and skills each uses
  profiles/<name>/domain.toml         profession rules: synonym concepts, blockers, forbidden terms (optional)

This module turns the pack into:
  * FactLines - every individual line the model may quote, each tagged with where it came from
  * Targets   - the resume variants, each with the set of lines that can actually appear on that resume
  * target_view() - how a job analysis looks from one resume variant (the deterministic rules below)

Rules applied by target_view (pure Python, re-applied on every run, never stored as a change):
  * a "met" verdict whose quote exists only in lines this resume variant cannot show -> counted as partial
  * a "met" verdict proven only by a limited-proficiency skill (novice, working knowledge, ...) -> partial
In-progress certificates are never turned into lines, so they can never be evidence.
"""
from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from schemas.models import FactBankV2, Phase2GapAnalysis, ResumeProfile

LIMITED_RE = re.compile(r"\((?:novice|beginner|basic|familiar|familiarity|working knowledge)\)|^basic\b", re.I)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


@dataclass
class FactLine:
    key: str            # bullet id (acd_001), or skill:<section>:<n>, ctx:<role_id>, edu:degree, cert:<n>
    text: str
    kind: str           # bullet | additional | context | skill | education | certification | language
    role_id: str = ""
    project: str = ""
    section: str = ""
    limited: bool = False

    @property
    def tailorable(self) -> bool:
        return self.kind in ("bullet", "additional")


@dataclass
class Target:
    target_id: str
    label: str
    matches_job_titles: list[str]
    role_order: list[str]
    selection: dict             # role_id -> {"lead_with", "then", "min_bullets", "max_bullets", ...}
    include_additional: list[str]
    skills_sections: list[str]
    pool: set[str]              # FactLine keys that can appear on this resume

    def title_match(self, job_title: str) -> float:
        """0..1: how well the job title matches one of this variant's typical titles."""
        t = _norm(job_title)
        best = 0.0
        for phrase in self.matches_job_titles:
            words = [w for w in re.findall(r"[a-z]{3,}", _norm(phrase))]
            if words:
                best = max(best, sum(1 for w in words if w in t) / len(words))
        return best


@dataclass
class Profile:
    name: str
    folder: Path
    fact_bank: dict
    resume_profile: dict
    lines: list[FactLine]
    targets: dict[str, Target]
    forbidden_terms: list[str] = field(default_factory=list)
    concepts: list[tuple[str, str]] = field(default_factory=list)
    sensitive_flag: str = ""
    sensitive_text: str = ""
    tool_terms: list[str] = field(default_factory=list)

    def __post_init__(self):
        self._by_key = {ln.key: ln for ln in self.lines}
        self._normed = [(_norm(ln.text), ln) for ln in self.lines]
        self._roles = {r["role_id"]: r for r in self.fact_bank.get("roles", [])}
        self.limited_terms = _limited_terms(self.lines)

    # --- lookups ---------------------------------------------------------
    def line(self, key: str) -> FactLine | None:
        return self._by_key.get(key)

    def role(self, role_id: str) -> dict:
        return self._roles.get(role_id, {})

    def lines_with(self, quote: str) -> list[FactLine]:
        """Every line that contains this quote word for word (ignoring case and spacing)."""
        q = _norm(quote)
        return [ln for n, ln in self._normed if q and q in n] if q else []

    def quotable(self) -> list[str]:
        """Every distinct line the model may quote."""
        return list(dict.fromkeys(ln.text for ln in self.lines))

    def default_target(self) -> Target:
        return next(iter(self.targets.values()))

    # --- what the model sees -------------------------------------------
    def prompt_text(self) -> str:
        out, seen = [], set()

        def emit(text: str, indent: str = "- ") -> None:
            if text not in seen:
                seen.add(text)
                out.append(f"{indent}{text}")

        out.append("EXPERIENCE:")
        for r in self.fact_bank.get("roles", []):
            out.append(f"{r.get('employer', '')} - {r.get('title', '')} ({r.get('start', '')} to {r.get('end', '')})")
            if r.get("context"):
                emit(r["context"])
            for b in r.get("bullets", []):
                emit(b["text"])
            for p in r.get("projects", []):
                if p.get("project_name"):
                    out.append(f"  Project: {p['project_name']}")
                for b in p.get("bullets", []):
                    emit(b["text"])
        for title, kind in (("SKILLS:", "skill"), ("EDUCATION:", "education"),
                            ("CERTIFICATIONS:", "certification"), ("LANGUAGES:", "language"),
                            ("ADDITIONAL:", "additional")):
            group = [ln for ln in self.lines if ln.kind == kind]
            if group:
                out += ["", title]
                for ln in group:
                    emit(ln.text)
        return "\n".join(out)

    @property
    def fact_hash(self) -> str:
        """Changes only when what the model sees changes (forces re-analysis); variant edits do not."""
        return hashlib.sha256(self.prompt_text().encode("utf-8")).hexdigest()


def _limited_terms(lines: list["FactLine"]) -> list[str]:
    """'CATIA V5 (novice)' -> ['CATIA V5', 'CATIA'];  'FEA — Nastran / Patran (working knowledge)' -> FEA, Nastran, Patran."""
    terms: list[str] = []
    for ln in lines:
        if not ln.limited:
            continue
        core = re.sub(r"\(.*?\)", " ", ln.text)
        core = re.sub(r"^\s*basic\s+", "", core, flags=re.I)
        for part in re.split(r"\s*(?:/|,|—|–|\s-\s)\s*", core):
            part = part.strip()
            if len(part) < 2:
                continue
            terms.append(part)
            first = part.split()[0]
            if len(part.split()) > 1 and len(first) >= 3 and sum(c.isupper() for c in first) >= 2:
                terms.append(first)                       # 'CATIA V5' also covers plain 'CATIA'
    return list(dict.fromkeys(terms))


# ------------------------------------------------------------------------- #
# Building a profile
# ------------------------------------------------------------------------- #

def _lines_from_fact_bank(fb: dict) -> list[FactLine]:
    lines: list[FactLine] = []
    for r in fb.get("roles", []):
        rid = r["role_id"]
        if r.get("context"):
            lines.append(FactLine(f"ctx:{rid}", r["context"], "context", role_id=rid))
        for b in r.get("bullets", []):
            lines.append(FactLine(b["id"], b["text"], "bullet", role_id=rid))
        for p in r.get("projects", []):
            for b in p.get("bullets", []):
                lines.append(FactLine(b["id"], b["text"], "bullet", role_id=rid, project=p.get("project_name", "")))
    for track, sections in (fb.get("skills") or {}).items():
        for sec, items in sections.items():
            for i, s in enumerate(items):
                lines.append(FactLine(f"skill:{track}.{sec}:{i}", s, "skill", section=f"{track}.{sec}",
                                      limited=bool(LIMITED_RE.search(s))))
    edu = fb.get("education") or {}
    if edu.get("degree"):
        text = ", ".join(str(x) for x in (edu["degree"], edu.get("institution"), edu.get("year")) if x)
        lines.append(FactLine("edu:degree", text, "education"))
    project = edu.get("final_year_project") or {}
    if project.get("description"):
        lines.append(FactLine("edu:project", project["description"], "education"))
    for i, c in enumerate(fb.get("certifications", [])):
        if str(c.get("status", "completed")).strip().lower() == "completed":
            text = " - ".join(x for x in (c.get("name", ""), c.get("issuer", "")) if x)
            lines.append(FactLine(f"cert:{i}", text, "certification"))
    for a in fb.get("additional", []):
        lines.append(FactLine(a["id"], a["text"], "additional"))
    for i, lang in enumerate(fb.get("languages", [])):
        lines.append(FactLine(f"lang:{i}", lang, "language"))
    return lines


def _targets(rp: dict, lines: list[FactLine], fb: dict) -> tuple[dict[str, Target], list[str]]:
    keys = {ln.key for ln in lines}
    role_ids = {r["role_id"] for r in fb.get("roles", [])}
    sections = {ln.section for ln in lines if ln.kind == "skill"}
    problems: list[str] = []
    targets: dict[str, Target] = {}
    always = {ln.key for ln in lines if ln.kind in ("certification", "language")} | ({"edu:degree"} & keys)

    for t in rp.get("target_roles", []):
        tid = t["target_id"]
        sel = t.get("bullet_selection", {})
        pool = set(always)
        for rid, s in sel.items():
            if rid not in role_ids:
                problems.append(f"{tid}: bullet_selection names unknown role '{rid}'")
            pool |= set(s.get("lead_with", [])) | set(s.get("then", []))
        for rid in t.get("role_order", []):
            if rid not in role_ids:
                problems.append(f"{tid}: role_order names unknown role '{rid}'")
            pool.add(f"ctx:{rid}")
        pool |= set(t.get("include_additional", []))
        for s in t.get("skills_sections_order", []):
            if s not in sections:
                problems.append(f"{tid}: skills section '{s}' does not exist in the Fact Bank")
        pool |= {ln.key for ln in lines if ln.kind == "skill" and ln.section in t.get("skills_sections_order", [])}
        if t.get("include_education_project"):
            pool.add("edu:project")
        unknown = sorted(k for k in pool if k not in keys and not k.startswith("ctx:"))
        if unknown:
            problems.append(f"{tid}: these ids are not in the Fact Bank: {', '.join(unknown)}")
        targets[tid] = Target(tid, t.get("label", tid), t.get("matches_job_titles", []), t.get("role_order", []),
                              sel, t.get("include_additional", []), t.get("skills_sections_order", []),
                              {k for k in pool if k in keys})
    return targets, problems


def _everything_target(lines: list[FactLine], fb: dict) -> Target:
    """Used when a pack has no resume_profile.json: one variant that may show every line."""
    sel = {r["role_id"]: {"lead_with": [b["id"] for b in r.get("bullets", [])] +
                          [b["id"] for p in r.get("projects", []) for b in p.get("bullets", [])]}
           for r in fb.get("roles", [])}
    return Target("default", "Default resume", [], [r["role_id"] for r in fb.get("roles", [])], sel,
                  [ln.key for ln in lines if ln.kind == "additional"],
                  sorted({ln.section for ln in lines if ln.kind == "skill"}), {ln.key for ln in lines})


def build_profile(name: str, fact_bank: dict, resume_profile: dict | None = None, domain: dict | None = None,
                  folder: Path | None = None) -> Profile:
    """Validate and assemble a profile from plain dictionaries (also used by the tests)."""
    try:
        FactBankV2.model_validate(fact_bank)
    except ValidationError as e:
        raise SystemExit(f"fact_bank.json does not have the expected structure:\n{e}")
    lines = _lines_from_fact_bank(fact_bank)
    ids = [ln.key for ln in lines]
    dupes = sorted({k for k in ids if ids.count(k) > 1})
    if dupes:
        raise SystemExit(f"fact_bank.json uses the same id more than once: {', '.join(dupes)}")
    if resume_profile:
        try:
            ResumeProfile.model_validate(resume_profile)
        except ValidationError as e:
            raise SystemExit(f"resume_profile.json does not have the expected structure:\n{e}")
        targets, problems = _targets(resume_profile, lines, fact_bank)
        if problems:
            raise SystemExit("resume_profile.json does not match fact_bank.json:\n  - " + "\n  - ".join(problems))
    else:
        targets = {}
    if not targets:
        t = _everything_target(lines, fact_bank)
        targets = {t.target_id: t}
    domain = domain or {}
    return Profile(name=name, folder=folder or Path("."), fact_bank=fact_bank, resume_profile=resume_profile or {},
                   lines=lines, targets=targets,
                   forbidden_terms=list(domain.get("forbidden_terms", [])),
                   concepts=[(c["requirement"], c["evidence"]) for c in domain.get("concepts", [])],
                   sensitive_flag=domain.get("sensitive_flag", ""), sensitive_text=domain.get("sensitive_text", ""),
                   tool_terms=list(domain.get("tool_terms", [])))


def load_profile(folder: Path) -> Profile:
    fb_path = folder / "fact_bank.json"
    if not fb_path.exists():
        raise SystemExit(f"No Fact Bank found at {fb_path}.\n"
                         f"Put your fact_bank.json (and resume_profile.json) in {folder}/, "
                         f"or choose another profile in config.toml.")

    def read_json(p: Path) -> dict:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise SystemExit(f"{p.name} is not valid JSON (line {e.lineno}, column {e.colno}): {e.msg}")

    rp_path, dom_path = folder / "resume_profile.json", folder / "domain.toml"
    domain = {}
    if dom_path.exists():
        try:
            with open(dom_path, "rb") as f:
                domain = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise SystemExit(f"domain.toml has a typing mistake: {e}")
    return build_profile(folder.name, read_json(fb_path), read_json(rp_path) if rp_path.exists() else None,
                         domain, folder)


# ------------------------------------------------------------------------- #
# Scoring view for one resume variant (pure Python)
# ------------------------------------------------------------------------- #

def _term_in(term: str, text: str) -> bool:
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(_norm(term))}(?![a-z0-9])", _norm(text)))


def target_view(analysis: Phase2GapAnalysis, profile: Profile, target: Target) -> Phase2GapAnalysis:
    flags, out = [], []
    for r in analysis.requirements_evaluation:
        if r.status == "met" and r.verbatim_quote:
            hits = [ln for q in [r.verbatim_quote, *(getattr(r, "extra_quotes", None) or [])]
                    for ln in profile.lines_with(q)]
            usable = [ln for ln in hits if ln.key in target.pool]
            if hits and not usable:
                r = r.model_copy(update={"status": "partial"})
                flags.append(f"Evidence is not on the '{target.label}' resume, counted as partial: "
                             f"{r.requirement_text[:70]}")
            elif usable and all(ln.limited for ln in usable):
                r = r.model_copy(update={"status": "partial"})
                flags.append(f"Only limited proficiency (e.g. novice / working knowledge), counted as partial: "
                             f"{r.requirement_text[:70]}")
            elif any(_term_in(t, r.requirement_text) for t in profile.limited_terms):
                term = next(t for t in profile.limited_terms if _term_in(t, r.requirement_text))
                r = r.model_copy(update={"status": "partial"})
                flags.append(f"Names '{term}', which you rated limited proficiency, counted as partial: "
                             f"{r.requirement_text[:60]}")
        out.append(r)
    return analysis.model_copy(update={"requirements_evaluation": out,
                                       "risk_flags": list(dict.fromkeys([*analysis.risk_flags, *flags]))})
