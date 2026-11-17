"""Tests for fetch_jobs.py. They never touch the network.

Run them with:  python3 -m unittest discover -s tests -v
"""
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import fetch_jobs as f  # noqa: E402


def job(title, text="", location="Worldwide", remote=True, intl=False, level="", **kw):
    return f.job("Test", kw.pop("org", "Org"), title, kw.pop("url", "https://example.test/" + title),
                 location=location, text=text, remote=remote, intl=intl, level=level, **kw)


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self):
        return b"{}"


class Run(unittest.TestCase):
    """Runs main() against fake sources in a temporary folder, capturing messages."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.sent = []
        patches = [
            mock.patch.object(f, "OUT", self.tmp / "jobs.json"),
            mock.patch.object(f, "OUTBOX", self.tmp / "outbox.json"),
            mock.patch.object(f, "check_direct_links", lambda old: ({}, [])),
            mock.patch.object(f.time, "sleep", lambda s: None),
            mock.patch.dict(f.PROFILE, {"notify_min_score": 0}),
            mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "fake", "TELEGRAM_CHAT_ID": "111,222",
                                         "DASHBOARD_URL": "https://dash.test/"}, clear=False),
            mock.patch.object(urllib.request, "urlopen", self.fake_urlopen),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("DEFER_NOTIFY", None)
        os.environ.pop("ADMIN_CHAT_ID", None)
        self.addCleanup(shutil.rmtree, self.tmp)

    def fake_urlopen(self, req, timeout=0):
        if getattr(req, "data", None):
            self.sent.append(urllib.parse.parse_qs(req.data.decode()))
        return FakeResponse()

    def run_day(self, sources):
        self.sent.clear()
        with mock.patch.object(f, "SOURCES", sources):
            f.main()
        return json.loads(f.OUT.read_text(encoding="utf-8"))

    def texts(self):
        return [m["text"][0] for m in self.sent]


# ---------------------------------------------------------------- scoring

class Scoring(unittest.TestCase):
    def keeps(self, j):
        return f.score(j) is not None

    def test_hospital_pharmacist_in_cairo_scores_highest(self):
        s, reasons, areas = f.score(job("Clinical Pharmacist", "ICU and inpatient wards.",
                                        location="Nasr City, Cairo, Egypt", remote=False))
        self.assertGreaterEqual(s, 80)
        self.assertIn("Hospital & clinical", areas)
        self.assertIn("Based in Greater Cairo", reasons)

    def test_remote_drug_safety_role_open_to_egypt_scores_well(self):
        s, reasons, areas = f.score(job("Pharmacovigilance Associate", "Open to candidates worldwide."))
        self.assertGreaterEqual(s, 70)
        self.assertIn("Drug safety", areas)

    def test_on_site_jobs_only_in_greater_cairo(self):
        for place in ["Cairo", "Giza, Egypt", "6th of October", "Sheikh Zayed", "Maadi, Cairo"]:
            with self.subTest(place=place):
                self.assertTrue(self.keeps(job("Hospital Pharmacist", location=place, remote=False)))
        for place in ["Alexandria, Egypt", "Mansoura, Egypt", "Riyadh", "Dubai"]:
            with self.subTest(place=place):
                self.assertFalse(self.keeps(job("Hospital Pharmacist", location=place, remote=False)))

    def test_egypt_without_a_city_is_kept_with_a_warning(self):
        s, reasons, _ = f.score(job("Hospital Pharmacist", location="Egypt", remote=False))
        self.assertIn("Check it's in Greater Cairo", reasons)

    def test_excluded_job_types_are_hidden(self):
        cases = [("Community Pharmacist", "", "Org"), ("Retail Pharmacist", "", "Org"), ("Pharmacist", "", "El Ezaby Pharmacy"),
                 ("Medical Representative", "", "Org"), ("Product Specialist", "", "Org"), ("Pharmacy Technician", "", "Org"),
                 ("Pharmacy Assistant", "", "Org"), ("Regulatory Affairs Specialist", "Travel up to 50%.", "Org")]
        for title, text, org in cases:
            with self.subTest(title=title, org=org):
                self.assertFalse(self.keeps(job(title, text, location="Cairo", remote=False, org=org)))

    def test_commercial_roles_are_hidden(self):
        for title in ["Marketing Manager - Hematology", "Access Manager – Oncology", "Haematology Medical Scientific Liaison",
                      "Customer Experience Partner – Rare Diseases", "MSL Oncology", "Brand Executive", "Business Development Lead"]:
            with self.subTest(title=title):
                self.assertFalse(self.keeps(job(title, "Pharmacist with hospital experience.",
                                                location="Cairo", remote=False, pharma=True)))

    def test_foreign_licences_are_hidden(self):
        for text in ["A valid DHA licence is required.", "SCFHS classification required.", "Must pass Prometric.",
                     "Active state pharmacy license required.", "GPhC registration required.", "Saudi licence preferred."]:
            with self.subTest(text=text):
                self.assertFalse(self.keeps(job("Clinical Pharmacist", text)))

    def test_remote_jobs_limited_to_other_countries_are_dropped(self):
        for text in ["Applicants must be located in the United States.",
                     "You must be authorized to work in the US.", "This is a US-based role."]:
            with self.subTest(text=text):
                self.assertFalse(self.keeps(job("Drug Safety Associate", text, location="Remote (check eligibility)")))
        self.assertFalse(self.keeps(job("Drug Safety Associate", location="Remote, India")))
        self.assertTrue(self.keeps(job("Drug Safety Associate", location="Remote, Egypt")))

    def test_quality_and_supply_count_only_in_a_pharma_setting(self):
        self.assertTrue(self.keeps(job("Quality Assurance Specialist", "GMP site", location="Cairo", remote=False, pharma=True)))
        self.assertTrue(self.keeps(job("Procurement Officer", "health products", location="Cairo", remote=False, intl=True)))
        self.assertFalse(self.keeps(job("Supply Chain Planner", "FMCG snacks", location="Cairo", remote=False)))

    def test_quality_and_supply_titles_need_a_medicines_context(self):
        for title, text in [("GHG Inventory Specialist", "Who we are: people who care about climate."),
                            ("Transport, Logistics and Supply Chain Intern", "Trade and development."),
                            ("Technical Validation Consultant", "Fraud research and drugs and crime.")]:
            with self.subTest(title=title):
                self.assertFalse(self.keeps(job(title, text, location="Home-based", intl=True)))
        self.assertTrue(self.keeps(job("Supply Chain Officer", "Procurement of essential medicines.",
                                       location="Cairo", remote=False, intl=True)))

    def test_software_qa_and_testing_jobs_are_hidden(self):
        for title in ["Associate QA Engineer", "QA Testers Wanted to Jump on an AI Assistant project"]:
            with self.subTest(title=title):
                self.assertFalse(self.keeps(job(title, "pharma customers, GMP", pharma=True)))

    def test_remote_jobs_tagged_with_one_place_are_dropped(self):
        for location in ["Lisbon, Lisbon, Portugal", "Brussels, Brussels, Belgium", "Massachusetts - Boston"]:
            with self.subTest(location=location):
                self.assertFalse(self.keeps(job("Drug Safety Associate", location=location, pharma=True)))
        for location, intl in [("Remote | London", True), ("Home-based (Brazil)", True), ("Remote | Caracas", True)]:
            with self.subTest(location=location):
                self.assertFalse(self.keeps(job("Health Supply Chain Specialist", "essential medicines",
                                                location=location, intl=intl, check_location=False)))
        self.assertTrue(self.keeps(job("Health Supply Chain Specialist", "essential medicines",
                                       location="Home-based", intl=True, check_location=False)))

    def test_regulatory_titles_need_a_medicines_context(self):
        self.assertFalse(self.keeps(job("Regulatory Affairs and Licensing Lead", "Crypto payments licences.")))
        self.assertTrue(self.keeps(job("Regulatory Affairs Specialist", "Drug registration with the Egyptian Drug Authority.",
                                       location="New Cairo", remote=False, pharma=True)))

    def test_other_jobs_at_pharma_companies_are_hidden(self):
        self.assertFalse(self.keeps(job("Legal Counsel - Egypt", "regulatory and hospital customers",
                                        location="Cairo, Egypt", remote=False, pharma=True)))

    def test_long_experience_is_kept_but_scored_lower(self):
        base = f.score(job("Regulatory Affairs Specialist", "Drug registration. 3 years of experience",
                           location="Cairo", remote=False))[0]
        scores = []
        for years in (5, 8):
            with self.subTest(years=years):
                long = f.score(job("Regulatory Affairs Specialist", f"Drug registration. {years} years of experience",
                                   location="Cairo", remote=False))
                self.assertIsNotNone(long)
                self.assertLess(long[0], base)
                scores.append(long[0])
        self.assertLess(scores[1], scores[0])  # 7+ years is lowered more than 5+

    def test_arabic_raises_and_native_english_lowers_the_score(self):
        base = f.score(job("Medical Information Pharmacist", "Hospital background"))[0]
        self.assertGreater(f.score(job("Medical Information Pharmacist", "Hospital background. Arabic speaking."))[0], base)
        lower = f.score(job("Medical Information Pharmacist", "Hospital background. Native English required."))
        self.assertTrue(lower is None or lower[0] < base)

    def test_unsuitable_and_ai_gig_titles_are_hidden(self):
        for title in ["Driver", "Registered Nurse", "Pharmacist - AI Trainer", "Dentist"]:
            with self.subTest(title=title):
                self.assertFalse(self.keeps(job(title, "pharmacist", location="Cairo", remote=False)))

    def test_senior_roles_and_other_languages_are_penalised(self):
        base = f.score(job("Drug Safety Associate"))[0]
        for title in ["German-speaking Drug Safety Associate", "Senior Drug Safety Specialist"]:
            with self.subTest(title=title):
                result = f.score(job(title))
                self.assertTrue(result is None or result[0] < base)

    def test_closed_jobs_are_dropped(self):
        self.assertFalse(self.keeps(job("Clinical Pharmacist", deadline="2000-01-01")))


class Facts(unittest.TestCase):
    def test_qualifications_years_contract_and_pay(self):
        facts = f.key_facts(job("Clinical Pharmacist",
                                "PharmD required. Licensed pharmacist. Minimum of 2 years of experience. "
                                "Salary EGP 15,000 per month. Full-time role."))
        self.assertEqual(facts, {"quals": "PharmD, Pharmacist licence", "years": 2,
                                 "contract": "Full-time", "pay": "EGP 15,000 per month"})

    def test_word_numbers_and_degree_wording(self):
        facts = f.key_facts(job("Regulatory Affairs Specialist",
                                "Bachelor of Pharmacy with two (2) years of relevant professional experience"))
        self.assertEqual(facts["quals"], "B.Pharm")
        self.assertEqual(facts["years"], 2)

    def test_money_without_a_unit_is_not_pay(self):
        self.assertNotIn("pay", f.key_facts(job("Consultant", "A project worth $5 million.")))


class Duplicates(unittest.TestCase):
    def test_same_un_post_on_two_sites_is_listed_once(self):
        a = {"title": "Procurement Officer", "location": "Cairo", "deadline": "2026-12-01T00:00:00Z", "intl": True}
        b = {"title": "Procurement Officer, P3", "location": "Cairo, Egypt", "deadline": "2026-12-02T04:59:59Z", "intl": True}
        later = {**a, "deadline": "2027-02-01T00:00:00Z"}
        self.assertEqual(f.merge_duplicates([a, b]), [a])
        self.assertEqual(len(f.merge_duplicates([a, later])), 2)  # a different post with the same title


class TimeLimit(unittest.TestCase):
    def test_requests_stop_once_the_run_is_out_of_time(self):
        with mock.patch.object(f, "DEADLINE", [f.time.time() - 1]), \
             mock.patch.object(urllib.request, "urlopen", lambda *a, **k: self.fail("should not fetch")):
            with self.assertRaises(f.OutOfTime):
                f.http("https://example.test/")


class ApplyEmail(unittest.TestCase):
    def test_only_addresses_given_for_applying(self):
        self.assertEqual(f.apply_email("Please send your CV to jobs@acme.org by Friday."), "jobs@acme.org")
        self.assertEqual(f.apply_email("For privacy questions contact privacy@acme.org."), "")
        self.assertEqual(f.apply_email("Apply: noreply@acme.org"), "")


# ---------------------------------------------------------------- daily run behaviour

class DailyRun(Run):
    def test_new_jobs_are_announced_once(self):
        src = {"A": lambda: [job("Clinical Pharmacist", "Hospital pharmacy")]}
        self.run_day(src)
        self.assertEqual(len(self.texts()), 2)  # one message per chat
        self.run_day(src)
        self.assertEqual(self.texts(), [])

    def test_alerts_link_to_the_dashboard_job(self):
        self.run_day({"A": lambda: [job("Clinical Pharmacist", "Hospital pharmacy")]})
        self.assertRegex(self.texts()[0], r'href="https://dash\.test/#job=[a-z0-9]+&amp;u=')

    def test_a_failed_source_keeps_its_jobs_and_does_not_reannounce(self):
        good = {"A": lambda: [job("Clinical Pharmacist", "Hospital pharmacy")]}

        def broken():
            raise RuntimeError("down")
        self.run_day(good)
        d = self.run_day({"A": broken})
        self.assertEqual(len(d["jobs"]), 1)
        self.run_day(good)
        self.assertEqual(self.texts(), [])

    def test_each_alert_gets_a_new_intro(self):
        intros = []
        for n in range(3):
            self.run_day({"A": lambda n=n: [job(f"Clinical Pharmacist number {n}", "Hospital pharmacy")]})
            intros.append(self.texts()[0].split("\n")[0])
        self.assertEqual(len(set(intros)), 3)

    def test_alerts_wait_for_the_website_when_deferred(self):
        os.environ["DEFER_NOTIFY"] = "1"
        self.addCleanup(os.environ.pop, "DEFER_NOTIFY", None)
        self.run_day({"A": lambda: [job("Clinical Pharmacist", "Hospital pharmacy")]})
        self.assertEqual(self.texts(), [])
        box = json.loads(f.OUTBOX.read_text(encoding="utf-8"))
        answers = iter(["2000-01-01T00:00:00Z", box["updated"]])
        with mock.patch.object(f, "http_json", lambda *a, **k: {"updated": next(answers)}):
            f.send_outbox()
        self.assertEqual(len(self.texts()), 2)


class ProblemAlerts(Run):
    def failing(self):
        raise RuntimeError("site changed")

    def test_source_failure_is_reported_once_after_two_days(self):
        os.environ["ADMIN_CHAT_ID"] = "999"
        good = lambda: [job("Clinical Pharmacist", "Hospital pharmacy")]  # noqa: E731
        self.run_day({"A": good, "B": good})
        self.run_day({"A": good, "B": self.failing})
        self.assertFalse(any("job source" in t for t in self.texts()))
        self.run_day({"A": good, "B": self.failing})
        admin = [m for m in self.sent if "job source" in m["text"][0]]
        self.assertEqual(len(admin), 1)
        self.assertEqual(admin[0]["chat_id"][0], "999")
        self.run_day({"A": good, "B": self.failing})
        self.assertFalse(any("job source" in t for t in self.texts()))
        self.run_day({"A": good, "B": good})
        self.assertTrue(any("Working again: B" in t for t in self.texts()))

    def test_no_matches_at_all_is_reported(self):
        self.run_day({"A": lambda: [job("Driver", location="Paris", remote=False)]})
        self.assertTrue(any("no matching jobs" in t for t in self.texts()))
        self.assertEqual(self.sent[-1]["chat_id"][0], "111")  # first chat when there's no admin chat


class LinkChecks(unittest.TestCase):
    def test_broken_link_reported_after_two_days_and_blocked_sites_skipped(self):
        def fake(req, timeout=0):
            if "cleopatra" in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO())
            if "bayt" in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 403, "blocked", {}, io.BytesIO())
            return FakeResponse()
        with mock.patch.object(urllib.request, "urlopen", fake):
            links, broken = f.check_direct_links({})
            self.assertEqual(broken, [])
            self.assertEqual(links["bayt"]["state"], "unknown")
            links, broken = f.check_direct_links(links)
            self.assertEqual([p["id"] for p, _ in broken], ["cleopatra"])
            links, broken = f.check_direct_links(links)
            self.assertEqual(broken, [])


# ---------------------------------------------------------------- project files

class ProjectFiles(unittest.TestCase):
    def test_profile_patterns_compile(self):
        profile = json.loads((ROOT / "profile.json").read_text(encoding="utf-8"))
        for key, value in profile.items():
            if isinstance(value, list) and value and isinstance(value[0], str) and key != "other_languages":
                for pattern in value:
                    re.compile(pattern)

    def test_direct_list_is_valid(self):
        groups = json.loads((ROOT / "docs" / "direct.json").read_text(encoding="utf-8"))
        ids = [p["id"] for g in groups for p in g["items"]]
        self.assertEqual(len(ids), len(set(ids)), "duplicate ids")
        for g in groups:
            for p in g["items"]:
                self.assertTrue(p["link"].startswith("https://"), p["id"])
                self.assertTrue(p["name"] and p["what"] and p["tip"], p["id"])

    def test_enough_intros_and_no_old_names(self):
        quotes = [q for q in (ROOT / "quotes.txt").read_text(encoding="utf-8").splitlines() if q.strip()]
        self.assertGreaterEqual(len(quotes), 30)
        self.assertEqual(len(quotes), len(set(quotes)), "duplicate intros")

    def test_every_area_and_pattern_compiles(self):
        profile = json.loads((ROOT / "profile.json").read_text(encoding="utf-8"))
        for name, cfg in profile["areas"].items():
            with self.subTest(area=name):
                self.assertIsInstance(cfg["weight"], int)
                for pattern in cfg["patterns"]:
                    re.compile(pattern)

    @unittest.skipUnless(shutil.which("node"), "Node.js not installed")
    def test_dashboard_script_has_no_syntax_errors(self):
        page = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
        script = re.search(r"<script>(.*)</script>", page, re.S).group(1)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
            tmp.write(script)
        self.addCleanup(os.unlink, tmp.name)
        result = subprocess.run(["node", "--check", tmp.name], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
