"""Offline tests for the guided setup (no model: proposals are given directly).   python -m unittest discover tests -v"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules import onboarding as ob  # noqa: E402

RESUME = """Senior Design Engineer | Contoso Engineering | Mar 2020 – Present
• Designed sheet metal brackets in Siemens NX for galley monuments.
• Released 40+ manufacturing drawings with GD&T per quarter.
Design Engineer | Fabrikam Motors | Jul 2016 – Feb 2020
• Modelled gearbox housings in Creo for casting and machining."""

PROPOSAL = {"roles": [
    {"employer": "Contoso Engineering", "title": "Senior Design Engineer", "start": "2020-03", "end": "Present",
     "bullets": ["Designed sheet metal brackets in Siemens NX for galley monuments.",
                 "Released 40+ manufacturing drawings with GD&T per quarter",
                 "Led a global team of 30 engineers across three continents."]},
    {"employer": "Fabrikam Motors", "title": "Design Engineer", "start": "2016-07", "end": "2020-02",
     "bullets": ["Modelled gearbox housings in Creo for casting and machining."]}],
    "skills": ["Siemens NX", "Creo"], "education": [{"degree": "B.E. Mechanical", "institution": "X", "year": "2016"}],
    "certifications": [], "languages": [], "additional": []}


class Verification(unittest.TestCase):
    def test_copied_bullet_is_verified(self):
        self.assertEqual(ob.verify_bullet("Released 40+ manufacturing drawings with GD&T per quarter", RESUME), "verified")

    def test_invented_bullet_is_flagged(self):
        self.assertEqual(ob.verify_bullet("Led a global team of 30 engineers across three continents.", RESUME), "reworded")

    def test_inflated_number_is_flagged(self):
        self.assertEqual(ob.verify_bullet("Released 400+ manufacturing drawings with GD&T per quarter and led audits", RESUME),
                         "reworded")


class Merging(unittest.TestCase):
    def test_two_resumes_merge_without_duplicates(self):
        other = {"roles": [{"employer": "Contoso Engineering", "title": "Sr. Design Engineer", "start": "2020-03",
                            "bullets": ["Designed sheet metal brackets in Siemens NX for galley monuments."]}],
                 "skills": ["siemens nx", "Teamcenter"]}
        m = ob.merge([(PROPOSAL, RESUME, "a.docx"), (other, RESUME, "b.docx")])
        self.assertEqual(len(m["roles"]), 2)
        self.assertEqual(len(m["roles"][0]["bullets"]), 3)
        self.assertEqual([s["text"] for s in m["skills"]], ["Siemens NX", "Creo", "Teamcenter"])
        self.assertEqual(m["roles"][0]["bullets"][2]["status"], "reworded")


class Building(unittest.TestCase):
    def test_confirmed_facts_become_a_valid_profile(self):
        tmp = tempfile.TemporaryDirectory()
        old = ob.PROFILES_DIR
        ob.PROFILES_DIR = Path(tmp.name)
        try:
            st = {"basics": {"name": "Priya Sharma", "email": "p@example.com"},
                  "confirmed": {"roles": [{"employer": "Contoso", "title": "Senior Design Engineer", "start": "Mar 2020",
                                           "end": "Present", "bullets": ["Designed brackets in Siemens NX."]},
                                          {"employer": "Fabrikam", "title": "Design Engineer", "start": "2016-07",
                                           "end": "2020-02", "bullets": ["Modelled housings in Creo."]}],
                                "skills": [{"text": "Creo", "level": "novice", "section": "tools"}],
                                "education": [{"degree": "B.E. Mechanical", "institution": "X", "year": "2016"}],
                                "certifications": [{"name": "Six Sigma", "status": "in progress"}],
                                "languages": [], "additional": []},
                  "variants": [{"label": "Design Engineer", "titles": "Design Engineer", "roles": []}],
                  "pack": "mechanical", "search": {"roles": ["Design Engineer"], "locations": ["Pune"], "country": "India"}}
            self.assertEqual(ob.write_pack("priya", st), [])
            from modules.profile_pack import load_profile
            p = load_profile(Path(tmp.name) / "priya")
            self.assertIn("Creo", p.limited_terms)                       # novice level is honoured
            self.assertNotIn("Six Sigma", " ".join(p.quotable()))        # in-progress cert is never evidence
            self.assertEqual(p.role(p.default_target().role_order[0])["start"], "2020-03")
        finally:
            ob.PROFILES_DIR = old
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
