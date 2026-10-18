from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

# --- PHASE 0 (legacy v1 Fact Bank; kept so older scripts such as phase0_init.py still import) ---

class FactBankItem(BaseModel):
    id: str
    category: str = Field(description="e.g., technical, leadership, infrastructure")
    text: str = Field(description="The verified bullet point or accomplishment.")


class MasterFactBank(BaseModel):
    candidate_name: str
    languages_and_frameworks: List[str]
    infrastructure_and_systems: List[str]
    methodologies: List[str]
    verified_bullets: List[FactBankItem]


# --- PHASE 0 (v2 profile pack: profiles/<name>/fact_bank.json and resume_profile.json) ---
# Only the fields the pipeline relies on are checked; any extra fields are allowed and kept.

class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


class FactBullet(_Open):
    id: str
    text: str
    category: str = ""


class FactProject(_Open):
    project_name: str = ""
    bullets: List[FactBullet] = Field(default_factory=list)


class FactRole(_Open):
    role_id: str
    employer: str = ""
    title: str = ""
    start: str = ""
    end: str = ""
    track: str = "both"
    context: str = ""
    bullets: List[FactBullet] = Field(default_factory=list)
    projects: List[FactProject] = Field(default_factory=list)


class FactCertification(_Open):
    name: str
    issuer: str = ""
    status: str = "completed"


class FactAdditional(_Open):
    id: str
    text: str
    category: str = ""


class FactBankV2(_Open):
    schema_version: str
    candidate_meta: Dict = Field(default_factory=dict)
    roles: List[FactRole]
    skills: Dict[str, Dict[str, List[str]]] = Field(default_factory=dict)
    education: Dict = Field(default_factory=dict)
    certifications: List[FactCertification] = Field(default_factory=list)
    additional: List[FactAdditional] = Field(default_factory=list)
    languages: List[str] = Field(default_factory=list)   # quotable evidence; shown on a resume only if the profile says so
    removed_items: Dict = Field(default_factory=dict)


class BulletSelection(_Open):
    lead_with: List[str] = Field(default_factory=list)
    then: List[str] = Field(default_factory=list)
    min_bullets: int = 0
    max_bullets: int = 99


class TargetRole(_Open):
    target_id: str
    label: str = ""
    matches_job_titles: List[str] = Field(default_factory=list)
    role_order: List[str] = Field(default_factory=list)
    bullet_selection: Dict[str, BulletSelection] = Field(default_factory=dict)
    skills_sections_order: List[str] = Field(default_factory=list)
    include_additional: List[str] = Field(default_factory=list)
    include_education_project: bool = False


class ResumeProfile(_Open):
    candidate: Dict = Field(default_factory=dict)
    target_roles: List[TargetRole]


# --- PHASE 1: Normalized Job Record ---

class NormalizedJob(BaseModel):
    job_id: str
    company: str
    title: str
    location: str
    application_url: str
    raw_description: str
    content_hash: str


# --- PHASE 2: Gap Analysis Contract (local model) ---

class RequirementEvidence(BaseModel):
    requirement_text: str
    type: str = Field(description="'hard_requirement' or 'preferred_qualification'")
    status: str = Field(description="'met', 'partial', or 'unmet'")
    verbatim_quote: Optional[str] = Field(None, description="Exact matching quote from Fact Bank, if met.")
    extra_quotes: List[str] = Field(default_factory=list,
                                    description="Up to 2 more exact quotes from OTHER Fact Bank lines that also prove it.")
    semantic_evidence: Optional[str] = Field(None, description="Contextual rationale.")


class Phase2GapAnalysis(BaseModel):
    requirements_evaluation: List[RequirementEvidence]
    missing_keywords: List[str]
    # Job keywords the candidate genuinely has the skill for but words differently.
    # Python verifies each one before Phase 4 may use it. Never filled from 'unmet' requirements.
    allowed_keywords: List[str] = Field(default_factory=list)
    risk_flags: List[str]


# --- PHASE 3: Deterministic triage result (written by local_analyzer.py) ---

class EvaluatedJob(BaseModel):
    job: NormalizedJob
    status: str = Field(description="'approved_for_phase4', 'needs_review', 'archived_below_threshold' or 'analysis_failed'")
    status_reasons: List[str] = Field(default_factory=list)
    overall_score: float = Field(0.0, description="0-100, for the best resume variant")
    hard_match_rate: Optional[float] = Field(None, description="0-100, None if the job has no hard requirements")
    preferred_match_rate: Optional[float] = Field(None, description="0-100, None if no preferred qualifications")
    analysis: Optional[Phase2GapAnalysis] = None          # what the model said, after the Python checks
    scored_analysis: Optional[Phase2GapAnalysis] = None   # the same, as seen by the chosen resume variant
    target_id: Optional[str] = None                       # chosen resume variant (target_roles in resume_profile)
    target_scores: Dict[str, float] = Field(default_factory=dict)  # overall score per resume variant
    model_used: str = ""
    fact_bank_hash: str = ""
    analyzed_at: str = ""
    error: Optional[str] = None


# --- PHASE 4: Drafting Contract ---

class TailoredBullet(BaseModel):
    original_id: str = Field(description="The ID of the source bullet from the Master Fact Bank.")
    rewritten_text: str = Field(description="The updated bullet incorporating allowed missing_keywords.")
    justification: str = Field(description="Explanation of how the allowed keyword was naturally integrated.")


class Phase4Draft(BaseModel):
    tailored_bullets: List[TailoredBullet]
    integrated_keywords: List[str]
