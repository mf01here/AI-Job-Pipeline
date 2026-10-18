"""Offline tests for Phase 5 (resume files and ledger).   python -m unittest discover tests -v"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules import resume_builder as rb  # noqa: E402


class Phase5(unittest.TestCase):
    def test_summary_is_built_only_from_approved_wording(self):
        cfg = {"headline_options": ["Mechanical Design Engineer  |  Siemens NX"],
               "summary_ingredients": ["12+ years across aerospace", "Currently designs cabin parts", "x", "y",
                                       "Designed the EAPS"], "summary_from": [0, 1, 4]}
        self.assertEqual(rb.summary_text(cfg), "Mechanical Design Engineer with 12+ years across aerospace. "
                                               "Currently designs cabin parts. Designed the EAPS.")

    def test_dates_and_labels(self):
        self.assertEqual(rb._month("2019-10"), "Oct 2019")
        self.assertEqual(rb._month("Present"), "Present")
        self.assertEqual(rb._section_label("design_track.cad_and_plm"), "CAD & PLM")

    def test_ledger_keeps_your_own_progress(self):
        rows = {"j1": {"job_id": "j1", "status": "applied", "applied_on": "2026-10-18", "notes": "called HR"}}
        draft = {"job": {"job_id": "j1", "company": "A", "title": "B", "application_url": "u", "content_hash": "h"},
                 "score": 70.0, "target": {"label": "Design"}}
        rb.update_ledger(rows, draft, rb.ROOT / "data" / "x.docx", None)
        self.assertEqual((rows["j1"]["status"], rows["j1"]["applied_on"], rows["j1"]["notes"]),
                         ("applied", "2026-10-18", "called HR"))


if __name__ == "__main__":
    unittest.main()
