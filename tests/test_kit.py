"""Offline tests: Apify and Google are faked, so nothing is spent or written outside a scratch folder."""
import contextlib, io, json, shutil, tempfile, unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from kit import apify, common, enrich, filter as flt, judge, search, sheet

ROOT = Path(__file__).resolve().parent.parent
NOW_MS = int(common.now().timestamp() * 1000)


def act_id(ms):
    """A LinkedIn activity id that encodes the time ms."""
    return str(ms << 22)


def post(i, text, name="Sam Owner", headline="Owner at Acme Cafe", hours_ago=3, kind="profile", slug=None):
    ms = NOW_MS - hours_ago * 3600_000 + i
    return {"type": "post", "id": act_id(ms), "linkedinUrl": f"https://www.linkedin.com/feed/update/urn:li:activity:{act_id(ms)}/",
            "content": text, "author": {"name": name, "info": headline, "type": kind,
                                         "publicIdentifier": slug or f"user{i}", "linkedinUrl": f"https://www.linkedin.com/in/{slug or f'user{i}'}"}}


def person(slug, headline, loc="London, England, United Kingdom", company="Acme Ltd"):
    return {"publicIdentifier": slug, "firstName": slug.title(), "lastName": "X", "headline": headline,
            "location": {"linkedinText": loc}, "currentPositions": [{"companyName": company}]}


class FakeBook:
    """Behaves like the Sheets API as the kit uses it: rows of strings, trailing blanks trimmed."""

    def __init__(self, tabs=None):
        self.data = {t: [list(r) for r in rows] for t, rows in (tabs or {}).items()}
        self.meta = None

    def tabs(self):
        return list(self.data)

    def read(self, tab):
        out = []
        for r in self.data[tab]:
            r = [str(x) for x in r]
            while r and r[-1] == "":
                r.pop()
            out.append(r)
        while out and not out[-1]:
            out.pop()
        return out

    def add_tab(self, tab):
        self.data[tab] = []

    def write(self, tab, managed, extra, old):
        rows = [list(m) + (list(extra[i]) if extra else []) for i, m in enumerate(managed)]
        self.data[tab] = rows

    def write_meta(self, rows):
        self.meta = rows

    def rows(self, tab):
        v = self.read(tab)
        return [dict(zip(v[0], r + [""] * (len(v[0]) - len(r)))) for r in v[1:]] if v else []


PROFILE = {
    "business": {"name": "Test Books", "what_we_sell": "bookkeeping", "ideal_buyer": "small business owners"},
    "timezone": "Europe/London",
    "tabs": [{"name": "Hot", "means": "asking for a bookkeeper"}, {"name": "Prospect", "means": "fits from people search"}],
    "rules": {"reject": [{"pattern": r"open ?to ?work", "why": "job seeker"}, "we offer"],
              "review": [{"pattern": r"\bsoftware\b", "why": "maybe software"}],
              "only_countries": [{"name": "United Kingdom", "code": "GB", "aliases": ["UK", "England", "London"]}],
              "exclude_countries": [{"name": "India", "code": "IN", "aliases": ["mumbai"]}],
              "country_basis": "company"},
    "enrich": {"enabled": True},
    "budget": {"per_run_usd": 1.0},
    "sheet_id": "x",
}
SEARCHES = [{"id": "P01", "type": "posts", "q": "\"looking for\" AND bookkeeper", "max_per_run": 10},
            {"id": "S01", "type": "people", "filters": {"currentJobTitles": ["Owner"]}, "pages_per_run": 1}]


class Scratch(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "config").mkdir()
        self.old = common.CONFIG, common.DATA
        common.CONFIG, common.DATA = self.dir / "config", self.dir / "data"
        self.set_profile(PROFILE)
        common.save(common.CONFIG / "searches.json", SEARCHES)
        (common.CONFIG / "apify_token.txt").write_text("apify_api_TEST", encoding="utf-8")
        self.calls = []
        self.results = {}
        self.usage = {"used": 0.5, "limit": 5.0, "start": "2026-10-01", "end": "2026-10-31"}
        patches = [mock.patch.object(apify, "run", self.fake_run), mock.patch.object(apify, "usage", lambda: dict(self.usage)),
                   mock.patch.object(apify, "actor_info", lambda actor: {"notice": "NONE"}),
                   mock.patch.object(apify, "input_schema", lambda actor: {"currentJobTitles": {}, "locations": {}}),
                   mock.patch.object(apify, "call", self.no_network)]
        for p in patches:
            p.start(); self.addCleanup(p.stop)

    def tearDown(self):
        common.CONFIG, common.DATA = self.old
        search.PEOPLE_FIELDS = None
        shutil.rmtree(self.dir, ignore_errors=True)

    def no_network(self, *a, **k):
        raise AssertionError("network call in an offline test")

    def set_profile(self, p):
        common.save(common.CONFIG / "profile.json", p)

    def fake_run(self, actor, inp, max_items, cap):
        self.calls.append((actor, inp, cap))
        items = self.results.get(actor, [])
        if callable(items):
            items = items(inp)
        return {"status": "SUCCEEDED", "usageTotalUsd": 0.01, "defaultDatasetId": "d"}, items

    def quiet(self, fn, *a):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            fn(*a)
        return out.getvalue()


class TestRules(unittest.TestCase):
    def test_query_rules(self):
        self.assertEqual(common.validate_query('"looking for" AND bookkeeper'), [])
        self.assertIn("lowercase operator (write AND, OR, NOT)", common.validate_query("a and b"))
        self.assertTrue(any("operators" in p for p in common.validate_query("a AND b AND c AND d AND e AND f AND g")))
        self.assertIn("unbalanced quotes", common.validate_query('"a AND b'))
        self.assertTrue(common.validate_query("“loving” AND b"))

    def test_countries(self):
        p = PROFILE
        self.assertEqual(common.outside("Mumbai, Maharashtra", "", p), "in India")
        self.assertEqual(common.outside("Austin, Texas, United States", "", p), "not in United Kingdom")
        self.assertEqual(common.outside("London, England", "", p), "")
        self.assertEqual(common.outside("", "", p), "")  # unknown is not evidence
        self.assertEqual(common.outside("", "US", p), "not in United Kingdom")
        self.assertEqual(common.outside("", "GB", p), "")
        self.assertEqual(common.country_in("Hyderabad, Pakistan", [{"name": "Pakistan"}, {"name": "India", "aliases": ["hyderabad"]}]), "Pakistan")

    def test_profile_check(self):
        self.assertEqual(common.check_profile(PROFILE), [])
        bad = dict(PROFILE, tabs=[{"name": "Review"}], sheet_id="")
        probs = common.check_profile(bad)
        self.assertTrue(any("built in" in x for x in probs) and any("sheet_id" in x for x in probs))
        self.assertTrue(common.check_profile(dict(PROFILE, rules={"reject": ["(unclosed"]})))

    def test_example_files_are_valid(self):
        search.PEOPLE_FIELDS = None  # field names are checked against the live schema elsewhere
        ex = json.loads((ROOT / "examples" / "profile.example.json").read_text(encoding="utf-8"))
        self.assertEqual(common.check_profile(ex), [])
        for s in json.loads((ROOT / "examples" / "searches.example.json").read_text(encoding="utf-8")):
            self.assertEqual(search.check_search(s), [], s["id"])

    def test_headline_company(self):
        self.assertEqual(flt.company_from_headline("Owner at Acme Cafe | Coffee lover"), "Acme Cafe")
        self.assertEqual(flt.company_from_headline("You're brilliant at what you do. I help you sell"), "")
        self.assertEqual(flt.company_from_headline("Partner at Cardinal Finance Partners"), "Cardinal Finance Partners")
        self.assertEqual(flt.company_from_headline("CFO @ Hewlett-Packard - ex PwC"), "Hewlett-Packard")

    def test_curly_apostrophes_match(self):
        lead = {"kind": "post", "text": "We’re hiring an accountant", "headline": "", "author_type": "profile",
                "location": "", "company": ""}
        p = dict(PROFILE, rules={"reject": [{"pattern": r"\bwe're hiring\b", "why": "job ad"}]})
        self.assertEqual(flt.score(lead, p)["route"], "reject")

    def test_formula_follows_row(self):
        self.assertEqual(sheet.moved_formula("=A5*2+$B$5+C$5", 2), "=A7*2+$B$5+C$5")
        self.assertEqual(sheet.moved_formula('=IF(A5="A5",1,0)', -1), '=IF(A4="A5",1,0)')
        self.assertEqual(sheet.moved_formula("plain A5", 3), "plain A5")

    def test_columns_match(self):
        x = {"id": "1", "kind": "post", "name": "", "headline": "", "profile": "", "url": "", "searches": []}
        self.assertEqual([k for k in judge.row(x, {}, "Hot") if not k.startswith("_")], sheet.COLS)


class TestFlow(Scratch):
    def search_and_filter(self, *args):
        out = self.quiet(search.main, list(args))
        out += self.quiet(flt.run)
        return out

    def test_search_filter_judge(self):
        self.results = {search.POSTS: [post(1, "Looking for a bookkeeper for my cafe in London"),
                                       post(2, "#OpenToWork looking for bookkeeping roles"),
                                       post(3, "We offer bookkeeping, DM me"),
                                       post(4, "Need software for bookkeeping, any tips?"),
                                       post(5, "old one", hours_ago=24 * 20)],
                        search.PEOPLE: [person("ann", "Owner at Ann's Bakery"), person("raj", "Founder", loc="Mumbai, India")]}
        self.search_and_filter()
        q = common.load(common.path("queue.json"), [])
        rej = common.read_jsonl(common.path("rejected.jsonl"))
        self.assertEqual(sorted(r["reason"] for r in rej), ["job seeker", "we offer"])
        by = {x["name"] if x["kind"] == "post" else x["id"]: x for x in q}
        self.assertEqual(len(q), 4)  # the 20-day-old post is never bought as new
        sw = next(x for x in q if "software" in x["text"])
        self.assertEqual(sw["suggest"]["tab"], "Review")
        self.assertEqual(by["person:raj"]["suggest"]["tab"], "Excluded")
        # a second run returns the same results: nothing new, the search is credited
        self.search_and_filter()
        self.assertEqual(len(common.load(common.path("queue.json"), [])), 4)
        # the judge
        keep = [{"id": q[0]["id"], "tab": "Hot", "notes": "asks"}]
        common.save(common.path("decisions.json"), keep + [{"id": "person:raj", "keep": False}, {"id": "zzz", "tab": "Nope"}])
        with self.assertRaises(SystemExit):
            self.quiet(judge.apply, None)  # one bad decision: nothing applied
        self.assertEqual(common.load(common.path("judged.json"), []), [])
        common.save(common.path("decisions.json"), keep + [{"id": "person:raj", "keep": False}])
        self.quiet(judge.apply, None)
        self.assertEqual(len(common.load(common.path("judged.json"), [])), 1)
        self.assertEqual(len(common.load(common.path("queue.json"), [])), 2)
        self.assertFalse(common.path("decisions.json").exists())

    def test_post_window_and_people_pages(self):
        self.results = {search.POSTS: [post(1, "Looking for a bookkeeper")],
                        search.PEOPLE: lambda inp: [person(f"p{inp['startPage']}{i}", "Owner") for i in range(25)]}
        self.search_and_filter()
        st = common.load(common.path("state.json"), {})
        self.assertIn("P01", st["since"])
        self.assertEqual(st["page"]["S01"], 2)
        first_win = self.calls[0][1]["postedLimitDate"]
        self.calls.clear()
        self.search_and_filter()
        later = next(c for c in self.calls if c[0] == search.POSTS)[1]["postedLimitDate"]
        self.assertGreater(later, first_win)  # the window moved up to the last run
        self.assertEqual(next(c for c in self.calls if c[0] == search.PEOPLE)[1]["startPage"], 2)
        # a short page: the people search is finished and skipped from then on
        self.results[search.PEOPLE] = lambda inp: [person("last", "Owner")]
        self.search_and_filter()
        self.assertTrue(common.load(common.path("state.json"), {})["finished"]["S01"])
        self.calls.clear()
        out = self.search_and_filter()
        self.assertNotIn(search.PEOPLE, [c[0] for c in self.calls])
        self.assertIn("finished", out)
        # changing the search starts it over
        common.save(common.CONFIG / "searches.json", [SEARCHES[0], dict(SEARCHES[1], filters={"locations": ["UK"]})])
        self.calls.clear()
        self.search_and_filter()
        self.assertEqual(next(c for c in self.calls if c[0] == search.PEOPLE)[1]["startPage"], 1)

    def test_empty_post_run_keeps_window(self):
        self.results = {search.POSTS: []}
        common.save(common.CONFIG / "searches.json", [SEARCHES[0]])
        self.search_and_filter()
        self.assertNotIn("P01", common.load(common.path("state.json"), {}).get("since", {}))

    def test_budget_waits_whole(self):
        self.set_profile(dict(PROFILE, budget={"per_run_usd": 0.06}))  # fits the post search (cap 0.04), not people (0.13)
        self.results = {search.POSTS: [post(1, "Looking for a bookkeeper")], search.PEOPLE: [person("a", "Owner")]}
        out = self.search_and_filter()
        self.assertEqual([c[0] for c in self.calls], [search.POSTS])
        st = common.load(common.path("state.json"), {})
        self.assertEqual(st["waiting"]["ids"], ["S01"])
        self.assertNotIn("S01", st.get("page", {}))
        self.assertIn("WAITING", out)
        for _, _, cap in self.calls:
            self.assertGreater(cap, 0)

    def test_test_mode_moves_nothing(self):
        self.results = {search.POSTS: [post(1, "Looking for a bookkeeper")], search.PEOPLE: [person("a", "Owner")]}
        out = self.search_and_filter("--test", "P01,S01", "--max", "5")
        st = common.load(common.path("state.json"), {})
        self.assertFalse(st.get("since") or st.get("page"))
        self.assertIn("TEST SAMPLE", out)
        self.assertEqual(self.calls[0][1]["maxPosts"], 5)
        self.assertEqual(len(common.load(common.path("queue.json"), [])), 2)  # paid-for results still reach the judge

    def test_scraper_refusal_keeps_progress(self):
        """The people scraper ends SUCCEEDED with nothing and a status message once a free account used its 10 runs."""
        common.save(common.CONFIG / "searches.json", [SEARCHES[1]])
        def refused(actor, inp, n, cap):
            return {"status": "SUCCEEDED", "statusMessage": "free user run limit reached", "usageTotalUsd": 0}, []
        with mock.patch.object(apify, "run", refused):
            out = self.search_and_filter()
        st = common.load(common.path("state.json"), {})
        self.assertIn("REFUSED", out)
        self.assertNotIn("S01", st.get("page", {}))
        self.assertNotIn("S01", st.get("finished", {}))

    def test_late_charges_still_counted(self):
        common.save(common.CONFIG / "searches.json", [SEARCHES[0]])
        self.results = {search.POSTS: [post(i, "Looking for a bookkeeper") for i in range(10)]}
        with mock.patch.object(apify, "run", lambda *a: ({"status": "SUCCEEDED", "usageTotalUsd": 0}, self.results[search.POSTS])):
            out = self.search_and_filter()
        self.assertIn("this run cost $0.020", out)

    def test_refused_run_stops(self):
        def refuse(*a):
            raise RuntimeError("POST /acts/x/runs -> 402: no credit")
        with mock.patch.object(apify, "run", refuse), self.assertRaises(SystemExit):
            self.quiet(search.main, [])


class TestEnrichAndSheet(Scratch):
    def judged(self, rows):
        common.save(common.path("judged.json"), rows)

    def lead(self, key, tab="Hot", name="Sam", slug="sam", company="", location="", found="2026-10-27T10:00:00"):
        x = {"id": key, "kind": "post", "name": name, "headline": f"Owner at {company or 'Acme'}", "company": company,
             "profile": f"https://www.linkedin.com/in/{slug}", "url": f"https://www.linkedin.com/feed/update/urn:li:activity:{key}/",
             "text": "=HYPERLINK(\"http://evil\")", "searches": ["P01"], "found_at": found, "location": location,
             "posted_ms": NOW_MS}
        return judge.row(x, {}, tab)

    def test_country_rule_and_pending(self):
        a, b, c = self.lead("1", slug="uk"), self.lead("2", slug="us"), self.lead("3", slug="in")
        self.judged([a, b, c])
        self.assertEqual(enrich.pending([a, b, c]), {"1", "2", "3"})  # unchecked leads stay off the sheet
        self.results = {
            enrich.PROFILE: lambda inp: [{"originalQuery": {"query": u}, "location": {"linkedinText": loc},
                                          "experience": [{"companyName": f"Co {s}", "companyLinkedinUrl": f"https://www.linkedin.com/company/co-{s}"}]}
                                         for u in inp["queries"] for s in [u.rsplit("/", 1)[1]]
                                         for loc in [{"uk": "Leeds, England", "us": "Leeds, England", "in": "Mumbai"}[s]]],
            enrich.COMPANY: lambda inp: [{"originalQuery": {"query": u}, "website": "https://co.example", "industries": [{"name": "Retail"}],
                                          "locations": [{"headquarter": True, "parsed": {"countryCode": cc, "text": t}}]}
                                         for u in inp["companies"] for s in [u.rsplit("-", 1)[1]]
                                         for cc, t in [{"uk": ("GB", "Leeds"), "us": ("US", "Austin, Texas"), "in": ("", "")}[s]]]}
        with mock.patch.object(apify, "prices", lambda actor, info=None: {}):
            self.quiet(enrich.run)
        rows = {r["Key"]: r for r in common.load(common.path("judged.json"), [])}
        self.assertEqual(rows["1"]["_tab"], "Hot")
        self.assertEqual(rows["1"]["Website"], "https://co.example")
        self.assertEqual(rows["2"]["_tab"], "Excluded")  # HQ in the US: outside the UK
        self.assertIn("not in United Kingdom", rows["2"]["Notes"])
        self.assertEqual(rows["3"]["_tab"], "Excluded")  # company page silent: the poster in Mumbai decides
        self.assertIn("in India", rows["3"]["Notes"])
        self.assertEqual(enrich.pending(list(rows.values())), set())

    def push(self, book, judged=None, accept=False):
        judged = judged if judged is not None else common.load(common.path("judged.json"), [])
        written = common.load(common.path("sheet_written.json"), {})
        reads, extras, n, _ = sheet.fold(judged, book, written, accept)
        snap = {}
        try:
            sheet.write(judged, book, reads, extras, enrich.pending(judged) - set(written), snap)
        except BaseException:
            common.save(common.path("sheet_written.json"), {**written, **snap})
            common.save(common.path("judged.json"), judged)
            raise
        common.save(common.path("judged.json"), judged)
        common.save(common.path("sheet_written.json"), snap)
        return n

    def test_failed_write_never_drops_a_moved_lead(self):
        self.set_profile(dict(PROFILE, enrich={"enabled": False}))
        self.judged([self.lead("50", name="Moves"), self.lead("51", name="Stays")])
        book = FakeBook()
        self.push(book)
        judged = common.load(common.path("judged.json"), [])
        next(r for r in judged if r["Key"] == "50")["_tab"] = "Excluded"  # the kit moves it
        common.save(common.path("judged.json"), judged)
        real = book.write
        def fail_on_excluded(tab, *a):
            if tab == "Excluded":
                raise RuntimeError("429 quota")
            return real(tab, *a)
        book.write = fail_on_excluded
        with self.assertRaises(RuntimeError):
            self.push(book)
        book.write = real
        self.push(book)
        self.assertEqual([r["Key"] for r in book.rows("Excluded")], ["50"])

    def test_hand_row_without_name_is_kept(self):
        self.set_profile(dict(PROFILE, enrich={"enabled": False}))
        self.judged([self.lead("60", name="A")])
        book = FakeBook()
        self.push(book)
        h = book.data["Hot"][0]
        row = [""] * len(h)
        row[h.index("Company")], row[h.index("Email")] = "Acme Ltd", "hi@acme.example"
        book.data["Hot"].append(row)
        self.push(book)
        self.push(book)
        self.assertIn("Acme Ltd", [r["Company"] for r in book.rows("Hot")])
        h = book.data["Hot"][0] + ["Mine"]
        book.data["Hot"][0] = h
        book.data["Hot"].append([""] * (len(h) - 1) + ["only my column"])
        with self.assertRaises(sheet.Stop):
            self.push(book)

    def test_second_fold_keeps_a_pipeline_move(self):
        self.set_profile(dict(PROFILE, enrich={"enabled": False}))
        self.judged([self.lead("70", name="X")])
        book = FakeBook()
        self.push(book)
        book.data["Prospect"] = [book.data["Hot"][0], book.data["Hot"].pop(1)]  # the client moves it
        judged = common.load(common.path("judged.json"), [])
        written = common.load(common.path("sheet_written.json"), {})
        _, _, _, first = sheet.fold(judged, book, written)
        self.assertEqual(judged[0]["_tab"], "Prospect")
        judged[0]["_tab"] = "Excluded"  # enrichment's country rule, between the two folds
        sheet.fold(judged, book, written, prior=first)
        self.assertEqual(judged[0]["_tab"], "Excluded")

    def test_dry_run_csv_is_safe_for_excel(self):
        self.set_profile(dict(PROFILE, enrich={"enabled": False}))
        self.judged([self.lead("80")])
        self.quiet(sheet.dry_run, common.load(common.path("judged.json"), []))
        text = (common.path("out") / "Hot.csv").read_text(encoding="utf-8-sig")
        self.assertIn("'=HYPERLINK", text)
        self.assertNotIn(",=HYPERLINK", text)

    def test_rule_without_pattern_is_refused(self):
        bad = dict(PROFILE, rules={"reject": [{"why": "job seeker"}], "must_match": ["(open"]})
        probs = common.check_profile(bad)
        self.assertTrue(any("non-empty pattern" in p for p in probs) and any("must_match" in p for p in probs))
        lead = {"kind": "post", "text": "none of the agencies helped", "headline": "", "author_type": "profile",
                "location": "", "company": ""}
        self.assertEqual(flt.score(lead, dict(PROFILE, rules={"reject": [{"why": "x"}]}))["route"], "keep")

    def test_enrich_counts_this_runs_search_spend(self):
        self.set_profile(dict(PROFILE, budget={"per_run_usd": 0.05}))
        self.judged([self.lead(str(i), slug=f"s{i}") for i in range(90, 96)])
        st = common.load(common.path("state.json"), {})
        st["last_run_spend"] = {"at": common.now().isoformat(timespec="seconds"), "usd": 0.04}
        common.save(common.path("state.json"), st)
        with mock.patch.object(apify, "prices", lambda actor, info=None: {}):
            out = self.quiet(enrich.run)
        self.assertIn("covers 1 of 6", out)  # $0.05 - $0.04 leaves room for one lead at $0.008

    def test_sheet_keeps_client_changes(self):
        self.set_profile(dict(PROFILE, enrich={"enabled": False}))
        self.judged([self.lead("11", name="A", found="2026-10-27T09:00:00"), self.lead("12", name="B"),
                     self.lead("13", name="C"), self.lead("14", name="D")])
        book = FakeBook()
        self.push(book)
        hot = book.rows("Hot")
        self.assertEqual([r["Name"] for r in hot], ["B", "C", "D", "A"])  # newest find first
        self.assertTrue(hot[0]["Post text"].startswith("="))  # written RAW: stays text, never a formula
        self.assertEqual(book.rows("Review"), [])
        # the client edits, moves, drops, adds a column and a row
        v = book.data["Hot"]
        h = v[0]
        col = lambda name: h.index(name)
        v[0] = h + ["Called?"]
        v[1][col("Notes")] = "call Monday"
        v[1] = v[1] + ["TRUE"]
        v[2][col("Status")] = "drop"
        moved = v.pop(3)
        book.data["Review"] = [h, moved]
        new = [""] * len(h)
        new[col("Name")], new[col("Profile")] = "Hand Added", "https://www.linkedin.com/in/hand"
        v.append(new)
        n = self.push(book)
        self.assertEqual(n, {"edits": 1, "moved": 1, "added": 1, "dropped": 1})
        hot = {r["Name"]: r for r in book.rows("Hot")}
        self.assertEqual(set(hot), {"B", "A", "Hand Added"})
        self.assertEqual(hot["B"]["Notes"], "call Monday")
        self.assertEqual(hot["B"]["Called?"], "TRUE")
        self.assertEqual(hot["Hand Added"]["Key"], "person:hand")
        self.assertEqual([r["Name"] for r in book.rows("Review")], ["D"])
        # a later pipeline change never overwrites the client's edit
        judged = common.load(common.path("judged.json"), [])
        next(r for r in judged if r["Key"] == "12")["Notes"] = "pipeline note"
        self.push(book, judged)
        self.assertEqual({r["Name"]: r for r in book.rows("Hot")}["B"]["Notes"], "call Monday")

    def test_sheet_stops_on_danger(self):
        self.set_profile(dict(PROFILE, enrich={"enabled": False}))
        self.judged([self.lead(str(i), name=f"N{i}") for i in range(20, 30)])
        book = FakeBook()
        self.push(book)
        before = json.dumps(book.data)
        # a copy of a row pasted into another tab
        book.data["Review"] = [book.data["Hot"][0], book.data["Hot"][1]]
        with self.assertRaises(sheet.Stop):
            self.push(book)
        book.data["Review"] = []
        # most of a tab vanished at once
        book.data["Hot"] = book.data["Hot"][:2]
        with self.assertRaises(sheet.Stop):
            self.push(book)
        self.push(book, accept=True)
        self.assertEqual(len(book.rows("Hot")), 1)
        # Key header renamed
        book.data["Hot"][0][book.data["Hot"][0].index("Key")] = "Id"
        with self.assertRaises(sheet.Stop):
            self.push(book)
        self.assertNotEqual(before, json.dumps(book.data))

    def test_moving_out_of_excluded_overrides(self):
        self.set_profile(dict(PROFILE, enrich={"enabled": False}))
        r = self.lead("40", location="Mumbai")
        self.judged([r])
        self.quiet(enrich.run)  # enrichment off: the rule runs on what the lead shows
        book = FakeBook()
        self.push(book)
        self.assertEqual([x["Key"] for x in book.rows("Excluded")], ["40"])
        row = book.data["Excluded"].pop(1)
        book.data["Hot"].append(row)
        self.push(book)
        self.quiet(enrich.run)
        self.push(book)
        self.assertEqual([x["Key"] for x in book.rows("Hot")], ["40"])


if __name__ == "__main__":
    unittest.main()
