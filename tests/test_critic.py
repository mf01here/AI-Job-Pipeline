"""Offline tests for the Phase 4 critique loop (simulated models).   python -m unittest discover tests -v"""
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules import cloud_generator as cg  # noqa: E402
from modules.profile_pack import build_profile  # noqa: E402
from schemas.models import EvaluatedJob, NormalizedJob, Phase2GapAnalysis, RequirementEvidence  # noqa: E402

FB = {"schema_version": "2.0", "roles": [{"role_id": "r1", "employer": "Acme", "title": "Engineer", "start": "2021-01",
      "end": "Present", "bullets": [
          {"id": "b1", "text": "Provide design guidance to peer teams in addition to own deliverables."},
          {"id": "b2", "text": "Release manufacturing drawings for galley brackets."},
          {"id": "b3", "text": "Run clash checks on large assemblies."}]}]}
RP = {"target_roles": [{"target_id": "t", "label": "Engineer", "role_order": ["r1"],
      "bullet_selection": {"r1": {"lead_with": ["b1", "b2", "b3"], "min_bullets": 3, "max_bullets": 3}}}]}
PROFILE = build_profile("t", FB, RP, {})


def job():
    a = Phase2GapAnalysis(requirements_evaluation=[
        RequirementEvidence(requirement_text="guidance to teams and deliverables", type="hard_requirement", status="met",
                            verbatim_quote="Provide design guidance to peer teams"),
        RequirementEvidence(requirement_text="manufacturing drawings", type="hard_requirement", status="met",
                            verbatim_quote="Release manufacturing drawings"),
        RequirementEvidence(requirement_text="clash checks on assemblies", type="hard_requirement", status="met",
                            verbatim_quote="Run clash checks")],
        missing_keywords=[], allowed_keywords=["technical support", "GD&T", "DMU"], risk_flags=[])
    j = NormalizedJob(job_id="1", company="X", title="Engineer", location="", application_url="", raw_description="x",
                      content_hash="h")
    return EvaluatedJob(job=j, status="approved_for_phase4", analysis=a, scored_analysis=a, target_id="t")


REWRITES = {"b1": "Provide technical support and design guidance to peer teams in addition to delivering own deliverables.",
            "b2": "Release manufacturing drawings with GD&T for galley brackets.",
            "b3": "Run DMU clash checks on large assemblies."}


def fake_model(reviews):
    def call(messages, temperature=None):
        if messages[0]["content"].startswith("You review"):
            call.critic_prompt = messages[1]["content"]
            return json.dumps({"reviews": reviews})
        ids = re.findall(r"- id: (\S+)", messages[1]["content"])
        return json.dumps({"tailored_bullets": [{"original_id": i, "rewritten_text": REWRITES[i]} for i in ids]})
    call.model_name = "fake"
    return call


class CritiqueLoop(unittest.TestCase):
    def setUp(self):
        self.old = cg.CRITIC_MODE
        cg.CRITIC_MODE = "local"

    def tearDown(self):
        cg.CRITIC_MODE = self.old

    def run_job(self, reviews):
        call = fake_model(reviews)
        res = cg.draft_job(job(), PROFILE, call, [])
        return {b["id"]: b for s in res["sections"] for b in s["bullets"]}, call

    def test_fix_revert_and_guarded_suggestion(self):
        rows, call = self.run_job([
            {"id": "b1", "verdict": "fix", "text": "Provide technical support and design guidance to peer teams, "
                                                    "in addition to own deliverables.", "reason": "repetition"},
            {"id": "b2", "verdict": "fix", "text": "Release 40 manufacturing drawings with GD&T for galley brackets.",
             "reason": "more specific"},
            {"id": "b3", "verdict": "revert", "reason": "keyword forced"}])
        self.assertIn("repeat the same root", call.critic_prompt)               # lint hint was passed on
        self.assertEqual(rows["b1"]["final"], "Provide technical support and design guidance to peer teams, "
                                              "in addition to own deliverables.")
        self.assertIn("improved", rows["b1"]["critic"])
        self.assertEqual(rows["b2"]["final"], REWRITES["b2"])                     # invented "40" rejected by guard
        self.assertIn("rejected by the guard", rows["b2"]["critic"])
        self.assertEqual(rows["b3"]["source"], "original")

    def test_safety_net_without_a_critic_answer(self):
        rows, _ = self.run_job([])                        # critic says nothing useful
        self.assertEqual(rows["b1"]["source"], "original")   # "delivering ... deliverables" caught by rules
        self.assertIn("repeat", rows["b1"]["critic"])
        self.assertEqual(rows["b2"]["final"], REWRITES["b2"])


class Lint(unittest.TestCase):
    def test_only_new_problems_count(self):
        self.assertTrue(cg.new_lint("Own design deliverables.", "Own delivering of design deliverables."))
        self.assertEqual(cg.new_lint("Design and design reviews.", "Design parts and run design reviews."), [])


if __name__ == "__main__":
    unittest.main()
