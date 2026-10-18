"""Offline tests for the Companies page logic (platform detection, store).   python -m unittest discover tests -v"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules import companies as comp  # noqa: E402


def page(html, url="https://careers.example.com/"):
    return lambda u: (url, html)


class Detection(unittest.TestCase):
    def test_direct_workday_link(self):
        r = comp.detect("https://ge.wd5.myworkdayjobs.com/GE_ExternalSite/job/Bengaluru/Lead-Engineer_R1")
        self.assertEqual((r["platform"], r["supported"]), ("workday", True))

    def test_careers_page_that_links_to_workday(self):
        html = '<a href="https://acme.wd3.myworkdayjobs.com/en-US/Acme_Careers">Search jobs</a>'
        r = comp.detect("https://www.acme.com/careers", fetch=page(html))
        self.assertEqual(r["platform"], "workday")
        self.assertIn("myworkdayjobs.com", r["portal"])

    def test_successfactors_site(self):
        html = '<link href="https://rmkcdn.successfactors.com/abc/style.css">'
        r = comp.detect("https://careers.example.com/", fetch=page(html))
        self.assertEqual((r["platform"], r["portal"]), ("successfactors", "successfactors:https://careers.example.com"))

    def test_unsupported_platform_is_named(self):
        r = comp.detect("https://jobs.example.com/", fetch=page('<script src="https://x.taleo.net/a.js"></script>'))
        self.assertEqual((r["name"], r["supported"]), ("Taleo", False))

    def test_avature_is_now_a_direct_portal(self):
        r = comp.detect("https://jobs.example.com/en_US/careers", fetch=page('<script src="https://x.avature.net/a.js">', "https://jobs.example.com/en_US/careers"))
        self.assertEqual((r["platform"], r["portal"]), ("avature", "avature:https://jobs.example.com/en_US/careers"))

    def test_unknown_page(self):
        r = comp.detect("https://example.com/", fetch=page("<html>hello</html>"))
        self.assertFalse(r["supported"])

    def test_unreachable_page(self):
        def boom(u):
            raise OSError("timed out")
        self.assertFalse(comp.detect("https://example.com/", fetch=boom)["supported"])


class Store(unittest.TestCase):
    def test_registry_and_watch_entries(self):
        old_load = comp.load
        comp.load = lambda profile=None: [
            {"name": "Siemens", "aliases": ["Siemens Energy"], "watch": True,
             "links": [{"supported": True, "portal": "https://a.wd3.myworkdayjobs.com/S1"},
                       {"supported": True, "portal": "successfactors:https://jobs.siemens-energy.com"},
                       {"supported": False, "platform": "avature", "portal": None}]},
            {"name": "Bharat Forge", "aliases": [], "watch": False, "links": []}]
        try:
            self.assertEqual(set(comp.registry_entries()), {"siemens", "siemens (2)"})
            names = [w["name"] for w in comp.watch_entries()]
            self.assertEqual(names, ["Siemens", "Bharat Forge"])   # no portal -> always watched
        finally:
            comp.load = old_load


if __name__ == "__main__":
    unittest.main()
