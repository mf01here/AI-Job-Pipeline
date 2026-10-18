"""
Offline tests for the zero-hallucination guards. No model, no internet, a few seconds.

    python -m unittest discover tests -v

Uses a tiny fictional candidate, so it never depends on anyone's real profile.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modules import local_analyzer as la  # noqa: E402
from modules.cloud_generator import check_bullet, plan_resume  # noqa: E402
from modules.profile_pack import build_profile, target_view  # noqa: E402
from schemas.models import EvaluatedJob, NormalizedJob, Phase2GapAnalysis, RequirementEvidence  # noqa: E402

FACT_BANK = {
    "schema_version": "2.0",
    "roles": [
        {"role_id": "acme_design", "employer": "Acme", "title": "Engineer", "start": "2020-01", "end": "Present",
         "track": "design", "context": "Aircraft seat structures.",
         "bullets": [{"id": "d1", "text": "Designed sheet metal brackets in Siemens NX for 20+ part assemblies."},
                     {"id": "d2", "text": "Raised ECRs and ECNs in Teamcenter for drawing revisions."}]},
        {"role_id": "acme_support", "employer": "Acme", "title": "Analyst", "start": "2020-01", "end": "Present",
         "track": "support",
         "bullets": [{"id": "s1", "text": "Automated KPI reporting in Looker Studio for the operations team."}]},
    ],
    "skills": {"design_track": {"cad": ["Siemens NX", "CATIA V5 (novice)"]},
               "support_track": {"tools": ["Looker Studio"]}},
    "education": {"degree": "Bachelor of Engineering, Mechanical Engineering", "institution": "Test College",
                  "year": 2012},
    "languages": ["English (fluent)"],
    "certifications": [{"name": "Done Cert", "status": "completed"},
                       {"name": "Future Cert", "status": "in progress"}],
}
RESUME_PROFILE = {"target_roles": [
    {"target_id": "design", "label": "Design", "role_order": ["acme_design"],
     "bullet_selection": {"acme_design": {"lead_with": ["d1"], "then": ["d2"], "min_bullets": 1, "max_bullets": 2}},
     "skills_sections_order": ["design_track.cad"]},
    {"target_id": "support", "label": "Support", "role_order": ["acme_support"],
     "bullet_selection": {"acme_support": {"lead_with": ["s1"], "min_bullets": 1, "max_bullets": 1}},
     "skills_sections_order": ["support_track.tools"]},
]}
PROFILE = build_profile("test", FACT_BANK, RESUME_PROFILE, {"forbidden_terms": ["Single Aisle"],
                                                             "tool_terms": ["CATIA", "Siemens NX", "NX", "Python"]})
la.apply_domain(PROFILE)


def req(text, status="met", quote=None, rtype="hard_requirement", extra=None):
    return RequirementEvidence(requirement_text=text, type=rtype, status=status, verbatim_quote=quote,
                               extra_quotes=extra or [])


def analysis(*reqs):
    return Phase2GapAnalysis(requirements_evaluation=list(reqs), missing_keywords=[], allowed_keywords=[], risk_flags=[])


class Phase4Guard(unittest.TestCase):
    ORIG = "Designed sheet metal brackets in Siemens NX for 20+ part assemblies."

    def test_accepts_honest_rewrite_with_new_verb(self):
        self.assertEqual(check_bullet(self.ORIG, "Engineered sheet metal brackets in Siemens NX for 20+ part assemblies.", [], []), [])

    def test_rejects_new_number(self):
        self.assertTrue(check_bullet(self.ORIG, "Designed sheet metal brackets in Siemens NX for 40+ part assemblies.", [], []))

    def test_rejects_new_tool_mid_sentence(self):
        self.assertTrue(check_bullet(self.ORIG, "Designed sheet metal brackets in Siemens NX and ANSYS for 20+ part assemblies.", [], []))

    def test_rejects_new_tool_as_first_word(self):
        self.assertTrue(check_bullet(self.ORIG, "CATIA-modelled sheet metal brackets in Siemens NX for 20+ part assemblies.", [], []))
        self.assertTrue(check_bullet(self.ORIG, "Ansys validated sheet metal brackets in Siemens NX for 20+ part assemblies.", [], []))

    def test_rejects_dropped_acronym(self):
        orig = "Raised ECRs and ECNs in Teamcenter for drawing revisions."
        self.assertTrue(check_bullet(orig, "Raised change requests in Teamcenter for drawing revisions.", [], []))

    def test_accepts_plural_change(self):
        orig = "Raised ECRs and ECNs in Teamcenter for drawing revisions."
        self.assertEqual(check_bullet(orig, "Raised ECR and ECN packages in Teamcenter for drawing revisions.", [], []), [])

    def test_rejects_forbidden_term(self):
        self.assertTrue(check_bullet(self.ORIG, "Designed sheet metal brackets in Siemens NX for 20+ part single aisle assemblies.",
                                     [], ["Single Aisle"]))

    def test_rejects_dropped_scope_qualifier(self):
        orig = "Provide composite design guidance to peer offshore teams when consulted, in addition to own deliverables."
        new = "Provide composite design guidance to peer offshore teams, in addition to own deliverables."
        self.assertTrue(check_bullet(orig, new, [], [], "present"))

    def test_rejects_tense_switch_in_current_role(self):
        orig = "Create and modify 3D models and release manufacturing drawings."
        self.assertTrue(check_bullet(orig, "Created and modified 3D models and released manufacturing drawings.", [], [], "present"))
        self.assertEqual(check_bullet(orig, "Create and modify 3D models, releasing manufacturing drawings.", [], [], "present"), [])

    def test_clean_text_restores_hyphens_and_spelling(self):
        from modules.cloud_generator import clean_text
        self.assertEqual(clean_text("modeling the A350\u2011XWB parts", "modelling the A350-XWB parts"),
                         "modelling the A350-XWB parts")

    def test_allows_verified_allowed_keyword(self):
        self.assertEqual(check_bullet(self.ORIG, "Designed sheet metal brackets in Siemens NX for 20+ part Assemblies (DFM).",
                                      ["DFM"], []), [])


class Phase2Enforcement(unittest.TestCase):
    DESC = "We need Siemens NX skills and experience with sheet metal brackets and assemblies. " * 5

    def test_invented_quote_is_downgraded(self):
        a = analysis(req("Siemens NX sheet metal", "met", "Designed titanium brackets in NX"))
        out = la.enforce(a, PROFILE.quotable(), self.DESC)
        self.assertEqual(out.requirements_evaluation[0].status, "partial")

    def test_real_quote_stays_met(self):
        a = analysis(req("Siemens NX sheet metal", "met", "Designed sheet metal brackets in Siemens NX"))
        out = la.enforce(a, PROFILE.quotable(), self.DESC)
        self.assertEqual(out.requirements_evaluation[0].status, "met")

    def test_invented_requirement_is_removed(self):
        a = analysis(req("Kubernetes cluster administration", "unmet"))
        out = la.enforce(a, PROFILE.quotable(), self.DESC)
        self.assertEqual(out.requirements_evaluation, [])


class ProfileRules(unittest.TestCase):
    def test_in_progress_certificate_is_never_evidence(self):
        texts = " ".join(PROFILE.quotable())
        self.assertIn("Done Cert", texts)
        self.assertNotIn("Future Cert", texts)

    def test_degree_is_quotable(self):
        self.assertTrue(PROFILE.lines_with("Bachelor of Engineering, Mechanical Engineering"))

    def test_language_is_evidence_on_every_variant(self):
        a = analysis(req("Fluent English", "met", "English (fluent)"))
        for t in PROFILE.targets.values():
            self.assertEqual(target_view(a, PROFILE, t).requirements_evaluation[0].status, "met")

    def test_novice_skill_counts_as_partial(self):
        a = analysis(req("CATIA V5", "met", "CATIA V5 (novice)"))
        self.assertEqual(target_view(a, PROFILE, PROFILE.targets["design"]).requirements_evaluation[0].status, "partial")

    def test_evidence_from_other_variant_counts_as_partial(self):
        a = analysis(req("Looker Studio reporting", "met", "Automated KPI reporting in Looker Studio"))
        self.assertEqual(target_view(a, PROFILE, PROFILE.targets["design"]).requirements_evaluation[0].status, "partial")
        self.assertEqual(target_view(a, PROFILE, PROFILE.targets["support"]).requirements_evaluation[0].status, "met")

    def test_best_variant_is_chosen_and_plan_uses_only_its_bullets(self):
        desc = ("Required: Looker Studio reporting, KPI automation, operations support. " * 8)
        job = NormalizedJob(job_id="1", company="X", title="Operations Analyst", location="", application_url="",
                            raw_description=desc, content_hash="h")
        a = analysis(req("Looker Studio reporting", "met", "Automated KPI reporting in Looker Studio"),
                     req("KPI automation", "met", "Automated KPI reporting"),
                     req("operations support", "partial"))
        ev = la.judge(EvaluatedJob(job=job, status="needs_review", analysis=a), PROFILE)
        self.assertEqual(ev.target_id, "support")
        _, sections, _, _ = plan_resume(ev, PROFILE)
        planned = [k for s in sections for k in s["keys"]]
        self.assertEqual(planned, ["s1"])



class ScoringRules(unittest.TestCase):
    def test_advanced_degree_is_not_met_by_bachelor(self):
        a = la.tighten(analysis(req("Advanced degree in Mechanical Engineering", "met", "Bachelor of Engineering")))
        self.assertEqual(a.requirements_evaluation[0].status, "unmet")

    def test_bachelor_or_master_is_met_by_bachelor(self):
        a = la.tighten(analysis(req("Bachelor's or Master's degree", "met", "Bachelor of Engineering")))
        self.assertEqual(a.requirements_evaluation[0].status, "met")

    def test_named_tool_needs_evidence_naming_it(self):
        a = la.tighten(analysis(req("Proficiency in CATIA V5 part design", "met", "Designed sheet metal brackets in Siemens NX")))
        self.assertEqual(a.requirements_evaluation[0].status, "partial")
        b = la.tighten(analysis(req("CATIA V5 or Siemens NX", "met", "Designed sheet metal brackets in Siemens NX")))
        self.assertEqual(b.requirements_evaluation[0].status, "met")

    def test_novice_skill_capped_whatever_line_is_quoted(self):
        a = analysis(req("CATIA part modelling", "met", "Designed sheet metal brackets in Siemens NX"))
        self.assertEqual(target_view(a, PROFILE, PROFILE.targets["design"]).requirements_evaluation[0].status, "partial")

    def test_few_hard_requirements_use_a_plain_average(self):
        a = analysis(req("Degree", "met", "Bachelor of Engineering"),
                     *[req(f"Preferred skill {i}", "unmet", rtype="preferred_qualification") for i in range(4)])
        _, _, overall = la.score(a)
        self.assertAlmostEqual(overall, 20.0)               # 1 of 5, not 70% from a single hard requirement

    def test_extra_quote_lets_each_variant_use_its_own_evidence(self):
        a = analysis(req("Change management and reporting", "met", "Automated KPI reporting in Looker Studio",
                         extra=["Raised ECRs and ECNs in Teamcenter"]))
        self.assertEqual(target_view(a, PROFILE, PROFILE.targets["design"]).requirements_evaluation[0].status, "met")

    def test_invented_extra_quote_is_removed(self):
        desc = "Change management and reporting with Teamcenter. " * 8
        a = analysis(req("Change management and reporting", "met", "Raised ECRs and ECNs in Teamcenter",
                         extra=["Led the global PLM transformation"]))
        out = la.enforce(a, PROFILE.quotable(), desc)
        self.assertEqual(out.requirements_evaluation[0].extra_quotes, [])



DESC_LONG = ("Required: Siemens NX modelling of sheet metal brackets and assemblies. Teamcenter change management. "
             "Preferred: reporting. ") * 4
GOOD_REQ = ('{"requirement_text":"Siemens NX sheet metal","type":"hard_requirement","status":"met",'
            '"verbatim_quote":"Designed sheet metal brackets in Siemens NX","extra_quotes":[],"semantic_evidence":"ok"}')


class DamagedReplies(unittest.TestCase):
    def _job(self, title="Design Engineer"):
        return NormalizedJob(job_id="1", company="X", title=title, location="", application_url="",
                             raw_description=DESC_LONG, content_hash="h")

    def test_broken_outer_json_is_salvaged_and_held_for_review(self):
        broken = ('{"requirements_evaluation":[' + GOOD_REQ + ',' + GOOD_REQ.replace("Siemens NX sheet metal",
                  "Teamcenter change management").replace('"ok"', '"uses 3" brackets"') + '],'
                  '"missing_keywords":["ANSYS",],"allowed_keywords":[],"risk_flags":[]}')

        def fake(messages, temperature=None):
            fake.last = {"cut_off": False, "thinking": False}
            return broken
        fake.model_name = "fake"
        a = la.analyze_job(self._job(), PROFILE, fake)
        self.assertGreaterEqual(len(a.requirements_evaluation), 1)
        self.assertTrue(any(f.startswith(la.INCOMPLETE_FLAG) for f in a.risk_flags))
        ev = la.judge(EvaluatedJob(job=self._job(), status="needs_review", analysis=a), PROFILE)
        self.assertNotEqual(ev.status, la.APPROVED)

    def test_cut_off_reply_is_salvaged(self):
        cut = '{"requirements_evaluation":[' + GOOD_REQ + ',{"requirement_text":"Teamcenter chan'
        data, ok = la.recover_analysis(cut)
        self.assertTrue(ok)
        self.assertEqual(len(data["requirements_evaluation"]), 1)

    def test_limited_tool_in_title_goes_to_review(self):
        reqs = [req("Siemens NX sheet metal", "met", "Designed sheet metal brackets in Siemens NX"),
                req("Teamcenter change management", "met", "Raised ECRs and ECNs in Teamcenter"),
                req("Siemens NX assemblies", "met", "Designed sheet metal brackets in Siemens NX")]
        job = self._job("Mechanical Design (CATIA) Engineer")
        ev = la.judge(EvaluatedJob(job=job, status="needs_review", analysis=analysis(*reqs)), PROFILE)
        self.assertEqual(ev.status, la.REVIEW)
        self.assertIn("CATIA", ev.status_reasons[0])


if __name__ == "__main__":
    unittest.main()
