"""
Offline tests for the scraper's filters (no internet): title exclusions and cross-source duplicates.

    python -m unittest discover tests -v
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules import scraper as sc  # noqa: E402

DESC = "Design composite cabin parts in Siemens NX and release drawings. " * 5


def rec(source, company, title, url, desc=DESC):
    return sc.raw_record(source, url, company, title, "Bengaluru", url, desc)


class TitleExclusions(unittest.TestCase):
    def setUp(self):
        words = ["intern", "internship", "apprentice", "student", "trainee"]
        sc.EXCLUDE_TITLE_RE = re.compile(r"(?<![a-z])(" + "|".join(words) + r")(?![a-z])", re.I)

    def tearDown(self):
        sc.EXCLUDE_TITLE_RE = None

    def test_internship_titles_are_excluded(self):
        self.assertTrue(sc.excluded_title_word("Internship - Physical Design & Integration"))
        self.assertTrue(sc.excluded_title_word("Graduate Trainee, Mechanical"))

    def test_normal_titles_are_kept(self):
        self.assertIsNone(sc.excluded_title_word("Senior Design Engineer - Internal Structures"))
        self.assertIsNone(sc.excluded_title_word("International Programs Engineer"))


class CrossSourceDuplicates(unittest.TestCase):
    def test_same_company_and_title_keeps_fuller_description(self):
        short = rec("naukri", "Collins Aerospace Pvt Ltd", "Design Engineer II", "https://naukri.com/a-123456789",
                    "Short text about the role. " * 4)
        full = rec("linkedin", "Collins Aerospace", "Design Engineer II", "https://linkedin.com/jobs/view/1")
        jobs, kept, dupes, _ = sc.dedupe([short, full])
        self.assertEqual((kept, dupes), (1, 1))
        self.assertTrue(jobs[0].job_id.startswith("linkedin"))

    def test_portal_copy_wins_a_tie(self):
        portal = rec("workday", "Collins Aerospace", "Design Engineer II", "https://x.wd5.myworkdayjobs.com/j/1")
        board = rec("linkedin", "Collins Aerospace", "Design Engineer II", "https://linkedin.com/jobs/view/2",
                    DESC + " Apply now.")
        jobs, kept, dupes, _ = sc.dedupe([portal, board])
        self.assertEqual((kept, dupes), (1, 1))
        self.assertTrue(jobs[0].job_id.startswith("workday"))

    def test_different_titles_are_both_kept(self):
        a = rec("linkedin", "Collins Aerospace", "Design Engineer II", "https://linkedin.com/jobs/view/3")
        b = rec("linkedin", "Collins Aerospace", "Stress Engineer", "https://linkedin.com/jobs/view/4",
                DESC + " Stress analysis focus.")
        self.assertEqual(sc.dedupe([a, b])[1], 2)


if __name__ == "__main__":
    unittest.main()


SEARCH_PAGE = """
<li><div class="base-card base-search-card job-search-card" data-entity-urn="urn:li:jobPosting:4011111111">
  <a class="base-card__full-link" href="https://in.linkedin.com/jobs/view/design-engineer-at-acme-4011111111?refId=x"></a>
  <h3 class="base-search-card__title"> Design Engineer </h3>
  <h4 class="base-search-card__subtitle"><a> Acme Aerospace </a></h4>
  <span class="job-search-card__location"> Bengaluru, Karnataka, India </span></div></li>
<li><div class="base-card base-search-card job-search-card" data-entity-urn="urn:li:jobPosting:4022222222">
  <a class="base-card__full-link" href="https://in.linkedin.com/jobs/view/intern-4022222222"></a>
  <h3 class="base-search-card__title">Design Intern</h3>
  <h4 class="base-search-card__subtitle"><a>Beta Ltd</a></h4>
  <span class="job-search-card__location">Bengaluru</span></div></li>
"""
POSTING_PAGE = """
<section class="description"><div class="show-more-less-html__markup">
<p>We need a mechanical design engineer with Siemens NX, Teamcenter and composite design experience.</p>
<ul><li>Release drawings</li><li>Run DMU clash checks</li></ul></div></section>
<ul><li class="description__job-criteria-item"><h3 class="description__job-criteria-subheader">Seniority level</h3>
<span class="description__job-criteria-text">Mid-Senior level</span></li></ul>
"""


class LinkedInParsing(unittest.TestCase):
    def test_search_cards(self):
        cards = sc.parse_linkedin_cards(SEARCH_PAGE)
        self.assertEqual([c["id"] for c in cards], ["4011111111", "4022222222"])
        self.assertEqual(cards[0]["title"], "Design Engineer")
        self.assertEqual(cards[0]["company"], "Acme Aerospace")
        self.assertEqual(cards[0]["url"], "https://in.linkedin.com/jobs/view/design-engineer-at-acme-4011111111")

    def test_posting_description_and_level(self):
        desc, level = sc.parse_linkedin_posting(POSTING_PAGE)
        self.assertIn("Siemens NX", desc)
        self.assertIn("Run DMU clash checks", desc)
        self.assertEqual(level, "Mid-Senior level")

    def test_fetch_reads_descriptions_and_skips_interns(self):
        calls = []

        def fake_get(url, params=None):
            calls.append(url)
            if "search" in url:
                return SEARCH_PAGE if params["start"] == 0 else ""
            return POSTING_PAGE
        old, sc._li_get = sc._li_get, fake_get
        sc.EXCLUDE_TITLE_RE = re.compile(r"(?<![a-z])(intern)(?![a-z])", re.I)
        try:
            recs = sc.fetch_linkedin("Design Engineer", "", "Bengaluru", 25)
        finally:
            sc._li_get, sc.EXCLUDE_TITLE_RE = old, None
        self.assertEqual([r["title"] for r in recs], ["Design Engineer"])
        self.assertGreater(len(recs[0]["description"]), 80)
        self.assertEqual(sum(1 for u in calls if "jobPosting" in u), 1)   # the intern's page is never fetched


SF_SEARCH = """
<table><tr class="data-row"><td><span class="jobTitle hidden-phone">
<a class="jobTitle-link" href="/job/Bengaluru-Mechanical-Design-Engineer/1234567/">Mechanical Design Engineer</a></span></td>
<td><span class="jobLocation">Bengaluru, KA, IN</span></td></tr>
<tr class="data-row"><td><a class="jobTitle-link" href="/job/Bengaluru-Mechanical-Design-Engineer/1234567/">Mechanical Design Engineer</a></td>
<td><span class="jobLocation">Bengaluru, KA, IN</span></td></tr></table>
"""
SF_JOB = """<div class="job"><span class="jobdescription"><p>Design aerostructures in CATIA and NX.</p>
<ul><li>Release drawings</li></ul></span></div>"""


class WatchListAndPortals(unittest.TestCase):
    def setUp(self):
        sc.ALIASES.clear()
        sc.ALIASES.update({"TCS": ["Tata Consultancy Services"], "L&T Technology Services": ["LTTS"]})

    def test_company_matches_name_and_aliases(self):
        self.assertTrue(sc.company_matches("Tata Consultancy Services Ltd", "TCS"))
        self.assertTrue(sc.company_matches("TCS", "TCS"))
        self.assertTrue(sc.company_matches("LTTS", "L&T Technology Services"))
        self.assertTrue(sc.company_matches("L&T Technology Services Limited", "L&T Technology Services"))

    def test_company_does_not_match_lookalikes(self):
        self.assertFalse(sc.company_matches("Metcs Engineering", "TCS"))
        self.assertFalse(sc.company_matches("Infosys BPM partner XYZ Recruiters", "Wipro"))

    def test_successfactors_registry_entry(self):
        self.assertEqual(sc.ats_from_url("successfactors:https://careers.wipro.com/"),
                         {"ats": "successfactors", "base": "https://careers.wipro.com"})

    def test_successfactors_parsing(self):
        rows = sc.parse_sf_search(SF_SEARCH)
        self.assertEqual(len(rows), 1)                       # desktop + phone copies of one row
        self.assertEqual(rows[0]["title"], "Mechanical Design Engineer")
        self.assertEqual(rows[0]["location"], "Bengaluru, KA, IN")
        self.assertIn("Design aerostructures", sc.parse_sf_job(SF_JOB))


SF_RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Jobs</title>
<item><title>Mechanical Design Engineer (Bengaluru, KA, IN, 560035)</title>
<link>https://careers.wipro.com/job/Bengaluru-Mechanical-Design-Engineer-KA/123456789/</link>
<description><![CDATA[<p>Design aerostructures in CATIA and Siemens NX. Release drawings, run DMU checks,
support manufacturing. Work with stress and suppliers across the full product lifecycle, from concept
to production, including change management in Teamcenter and BOM ownership for large assemblies.</p>]]></description>
<pubDate>Wed, 15 Oct 2026 08:00:00 GMT</pubDate></item></channel></rss>"""


class SuccessFactorsFeed(unittest.TestCase):
    def test_rss_parsing(self):
        jobs = sc.parse_sf_rss(SF_RSS)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["title"], "Mechanical Design Engineer")
        self.assertEqual(jobs[0]["location"], "Bengaluru, KA, IN, 560035")
        self.assertIn("Siemens NX", jobs[0]["description"])

    def test_bad_feed_gives_nothing(self):
        self.assertEqual(sc.parse_sf_rss("<html>not a feed</html"), [])


def _hcl_item(i, title, desc, link=None):
    return (f"<item><title><![CDATA[{title} (IN)]]></title><link>{link or f'https://careers.hcltech.com/job/x/{1000000 + i}/'}</link>"
            f"<description><![CDATA[<p>{desc}</p>]]></description></item>")


class CountryOnlyFeeds(unittest.TestCase):
    """HCLTech-style feeds: city search returns only a placeholder; items carry just '(IN)'."""

    def setUp(self):
        long = " Design and release mechanical assemblies with full lifecycle ownership." * 6
        self.pages = {
            "city": "<item><title><![CDATA[No jobs currently available - Check out our other opportunities]]></title>"
                    "<link>https://careers.hcltech.com/</link><description>x</description></item>",
            "any": _hcl_item(1, "Mechanical Design Engineer", "Location: Bangalore." + long)
                   + _hcl_item(2, "Stress Engineer", "Location: Chennai." + long)
                   + _hcl_item(3, "Solution Architect", "Bangalore." + long)}
        self.old_get, self.old_country = sc.SESSION.get, sc.COUNTRY_FILTER
        pages = self.pages

        class R:
            ok, status_code = True, 200

            def __init__(s, t): s.text = t

            def raise_for_status(s): pass

        def fake_get(url, params=None, **k):
            if params is None:
                return R("<html>Chennai office</html>")       # job page of the Chennai job
            key = "city" if "locationSearch" in params["keywords"] else "any"
            body = pages[key] if params.get("startrow", 0) == 0 else ""
            return R(f"<rss><channel>{body}</channel></rss>")
        sc.SESSION.get, sc.COUNTRY_FILTER = fake_get, "India"

    def tearDown(self):
        sc.SESSION.get, sc.COUNTRY_FILTER = self.old_get, self.old_country

    def test_falls_back_and_finds_the_city_in_the_text(self):
        recs = sc.fetch_successfactors({"base": "https://careers.hcltech.com"}, "hcltech", "Engineer", "Bengaluru", 10)
        self.assertEqual([r["title"] for r in recs], ["Mechanical Design Engineer"])
        self.assertEqual(recs[0]["location"], "Bengaluru, India")


class LinkedInBudgetAndRotation(unittest.TestCase):
    def test_rotation_gives_every_company_a_turn(self):
        watch = [{"name": n} for n in ("Cyient", "TCS", "Infosys", "Quest Global")]
        state = {"linkedin_watch_last": {"Cyient": "2026-10-17T07:00", "TCS": "2026-10-17T07:00"}}
        self.assertEqual(sc.pick_linkedin_watch(watch, state, 2), ["Infosys", "Quest Global"])  # never covered first
        state["linkedin_watch_last"].update({"Infosys": "2026-10-18T07:00", "Quest Global": "2026-10-18T07:00"})
        self.assertEqual(sc.pick_linkedin_watch(watch, state, 2), ["Cyient", "TCS"])

    def test_budget_stops_linkedin_and_keeps_what_was_read(self):
        old_get, old_pause, old_budget = sc._li_get, sc._li_pause, sc.LINKEDIN_BUDGET
        sc._li_pause = lambda: None
        sc.LINKEDIN_BUDGET, sc._LI_STATE["used"] = 3, 0     # 1 search page + 2 job pages
        sc.LI_RUN["blocked"] = False

        class R:
            status_code = 200

            def __init__(s, t): s.text = t

        pages = {"search": SEARCH_PAGE.replace("Design Intern", "Stress Engineer")}
        sc.requests.get, real = (lambda url, **k: R(pages["search"] if "search" in url else POSTING_PAGE)), sc.requests.get
        sc._li_get = old_get
        try:
            out = []
            sc.collect_aggregators("Design Engineer", "", ["Bengaluru"], ["linkedin"], "India", 25, out)
        finally:
            sc.requests.get, sc._li_pause, sc.LINKEDIN_BUDGET = real, old_pause, old_budget
            sc._LI_STATE["used"] = 0
        # 2 search requests + 1 job page fit in the budget: one full job, the other kept as a lead (no description)
        self.assertEqual(len(out), 2)
        self.assertEqual(sorted(len(r["description"]) > 80 for r in out), [False, True])


class TopUpMode(unittest.TestCase):
    def test_top_up_keeps_existing_and_adds_only_new(self):
        old = rec("workday", "GE Aerospace", "Rotor Design Engineer", "https://ge.wd5.myworkdayjobs.com/j/1")
        same_again = rec("linkedin", "GE Aerospace", "Rotor Design Engineer", "https://linkedin.com/jobs/view/9")
        new = rec("linkedin", "Quest Global", "Concept Design Engineer", "https://linkedin.com/jobs/view/8",
                  DESC + " Concept work.")
        jobs, kept, dupes, _ = sc.dedupe([old, same_again, new])
        self.assertEqual((kept, dupes), (2, 1))
        self.assertTrue(jobs[0].job_id.startswith("workday"))
