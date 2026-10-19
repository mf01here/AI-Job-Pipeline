"""Offline tests for the macOS scheduler (no launchctl calls are made).   python -m unittest discover tests -v"""
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules import scheduler as sch  # noqa: E402


class FakeMac(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (sch.AGENTS, sch._is_mac, sch._launchctl, sch.read_schedule)
        sch.AGENTS = Path(self.tmp.name)
        sch._is_mac = lambda: True
        self.calls = []

        class R:
            returncode = 0
            stdout = stderr = ""
        sch._launchctl = lambda *a: self.calls.append(a) or R()

    def tearDown(self):
        sch.AGENTS, sch._is_mac, sch._launchctl, sch.read_schedule = self.old
        self.tmp.cleanup()

    def test_morning_run_plist(self):
        sch.read_schedule = lambda profile=None: {"enabled": True, "time": "06:45", "days": list(sch.DAYS)}
        ok, msg = sch.apply_schedule()
        self.assertTrue(ok, msg)
        spec = plistlib.loads((sch.AGENTS / f"{sch.RUN_LABEL}.plist").read_bytes())
        self.assertEqual(spec["StartCalendarInterval"], {"Hour": 6, "Minute": 45})
        self.assertEqual(spec["ProgramArguments"][:2], ["/usr/bin/caffeinate", "-i"])
        self.assertTrue(spec["ProgramArguments"][3].endswith("orchestrator.py"))
        self.assertEqual(spec["ProgramArguments"][-2:], ["--trigger", "scheduled"])

    def test_chosen_days_only(self):
        sch.read_schedule = lambda profile=None: {"enabled": True, "time": "07:15", "days": ["mon", "wed", "fri"]}
        ok, msg = sch.apply_schedule()
        spec = plistlib.loads((sch.AGENTS / f"{sch.RUN_LABEL}.plist").read_bytes())
        self.assertEqual(spec["StartCalendarInterval"], [{"Weekday": 1, "Hour": 7, "Minute": 15},
                                                         {"Weekday": 3, "Hour": 7, "Minute": 15},
                                                         {"Weekday": 5, "Hour": 7, "Minute": 15}])
        self.assertIn("Mon, Wed, Fri", msg)

    def test_day_labels(self):
        self.assertEqual(sch.days_label(sch.DAYS), "every day")
        self.assertEqual(sch.days_label(["mon", "tue", "wed", "thu", "fri"]), "weekdays (Mon-Fri)")
        self.assertEqual(sch.days_label([]), "every day")            # nothing picked = every day, never "never"

    def test_turning_off_removes_it(self):
        sch.read_schedule = lambda profile=None: {"enabled": True, "time": "07:00", "days": list(sch.DAYS)}
        sch.apply_schedule()
        sch.read_schedule = lambda profile=None: {"enabled": False, "time": "07:00", "days": list(sch.DAYS)}
        sch.apply_schedule()
        self.assertFalse((sch.AGENTS / f"{sch.RUN_LABEL}.plist").exists())

    def test_background_control_centre(self):
        ok, _ = sch.set_background(True)
        spec = plistlib.loads((sch.AGENTS / f"{sch.WEB_LABEL}.plist").read_bytes())
        self.assertTrue(ok and spec["KeepAlive"] and spec["RunAtLoad"])
        self.assertTrue(spec["ProgramArguments"][-1].endswith("app.py"))


class Passcode(unittest.TestCase):
    def test_rejects_short_codes(self):
        self.assertFalse(sch.set_passcode("12")[0])


if __name__ == "__main__":
    unittest.main()
