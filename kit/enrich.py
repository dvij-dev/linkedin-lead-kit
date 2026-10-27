"""Fill Company, Location, Website, Industry and Company size for judged leads, and apply the client's country rule.

  python lead.py enrich              # leads in profile.json enrich.tabs, within the budget
  python lead.py enrich --dry-run    # how many lookups it would buy, and the worst case

On only when profile.json has "enrich": {"enabled": true}. Each person gets one profile lookup
(harvestapi/linkedin-profile-scraper: current company, its LinkedIn page, location), and each company one company
lookup (harvestapi/linkedin-company: website, industry, size, headquarters), both cached in data/enrich_cache.json so
nobody is paid for twice. Never looks up emails. Only blank cells are filled, plus a Company that was only guessed
from the headline; a cell the client edited in the sheet is never touched.

Country rule (profile.json rules.exclude_countries and/or rules.only_countries, rules.country_basis):
- company (default): the company's LinkedIn headquarters decides; when its page does not say, a country in the company
  name; then the poster's own location.
- poster: where the poster lives decides.
- either: the lead is excluded when the company or the poster is in one of those countries.
A lead moved to Excluded gets the evidence in Notes. A lead the client moved out of Excluded in the sheet, or whose
Status or Notes they edited, is never moved again. When nothing says where a lead is, it stays (rules.exclude_unknown:
true moves it to Excluded instead). With enrichment off the rule still runs on what the lead already shows.
While enrichment is on, a lead still waiting for its lookups is kept off the sheet (pending()), so a lead the country
rule would exclude is never shown unchecked.
"""
import argparse, re, sys
from kit import apify, common
from kit.common import load, save, now, slug_of

PROFILE, COMPANY = "harvestapi~linkedin-profile-scraper", "harvestapi~linkedin-company"
EVENT = {PROFILE: "profile", COMPANY: "apify-default-dataset-item"}
FALLBACK_PRICE = 0.004
BATCH = 10  # the free-plan cap on these actors' batch size


def cache():
    c = load(common.path("enrich_cache.json"), {})
    c.setdefault("profiles", {}); c.setdefault("companies", {})
    return c


def settings(p=None):
    e = (p or common.profile()).get("enrich") or {}
    return bool(e.get("enabled")), e.get("tabs") or (common.lead_tabs(p) + ["Review"])


def pkey(r):
    s = slug_of(r.get("Profile"))
    return f"{s[0]}:{s[1]}" if s else None


def wanted(r, tabs):
    return r.get("_tab") in tabs and pkey(r) and not r.get("_manual")


def positions(it):
    """Current jobs from a profile: experience with no end date, else currentPosition."""
    exp = [x for x in (it.get("experience") or []) if isinstance(x, dict)
           and str((x.get("endDate") or {}).get("text") or "Present").strip().lower() == "present"]
    pos = [{"company": (x.get("companyName") or "").strip(), "url": x.get("companyLinkedinUrl") or ""} for x in exp]
    return pos or [{"company": (x.get("companyName") or "").strip(), "url": x.get("companyLinkedinUrl") or ""}
                   for x in (it.get("currentPosition") or []) if isinstance(x, dict)]


def norm(t):
    return " ".join(re.sub(r"[^\w&]+", " ", str(t or "").lower()).split())


def choose(prof, r):
    """{company, url}: the current job the headline or post names, else the first current job. A company page is its
    own company."""
    if (slug_of(r.get("Profile")) or ("",))[0] == "company":
        return {"company": r.get("Name", ""), "url": r.get("Profile", "")}
    pos = [x for x in prof.get("positions") or [] if x.get("company")]
    ctx = f" {norm(r.get('Headline'))} {norm(r.get('Post text'))} {norm(r.get('Company'))} "
    named = next((x for x in pos if f" {norm(x['company'])} " in ctx), None)
    return named or (pos[0] if pos else {"company": "", "url": r.get("_company_url", "")})


def hq_of(it):
    """(country code, place) of a company's headquarters, '' when unknown."""
    locs = [x for x in (it.get("locations") or []) if isinstance(x, dict)]
    cc = lambda x: str((x.get("parsed") or {}).get("countryCode") or x.get("country") or "").upper()
    h = next((x for x in locs if x.get("headquarter")), None)
    if h is None and locs and len({cc(x) for x in locs}) == 1:
        h = locs[0]
    h = h or {}
    return cc(h), (h.get("parsed") or {}).get("text") or h.get("city") or ""


def verdict(r, prof, comp, p=None):
    """-> (rule, evidence) when the country rule excludes this lead, else ('', '')."""
    if not common.country_rule_on(p):
        return "", ""
    basis = common.rules(p).get("country_basis", "company")
    home = (prof or {}).get("location") or r.get("Location") or ""
    by_poster = common.outside(home, "", p)
    poster_why = f"the poster is {by_poster} ({home})"
    known_company = bool(comp and (comp.get("hq") or comp.get("hq_text")))
    if known_company:
        by_company = common.outside(comp.get("hq_text"), comp.get("hq"), p)
        why = f"its LinkedIn HQ is {by_company} ({comp.get('hq_text') or comp.get('hq')})"
    else:  # only an excluded country named in the company's own name counts here
        hit = common.country_in(r.get("Company"), common.excluded_countries(p))
        by_company, why = (f"in {hit}", f"the company name '{r.get('Company')}'") if hit else ("", "")
    if basis == "poster":
        return ("country", poster_why) if by_poster else ("", "")
    if by_company:
        return "country", why
    if by_poster and (basis == "either" or not known_company):
        return "country", poster_why if basis == "either" else f"the company page does not say where it is; {poster_why}"
    if not known_company and not home.strip() and common.rules(p).get("exclude_unknown"):
        return "location unknown", "neither the company page nor the poster's profile says where they are"
    return "", ""


def pending(judged, p=None, c=None):
    """Keys of leads still waiting for their lookups while enrichment and a country rule are on: kept off the sheet."""
    on, tabs = settings(p)
    if not on or not common.country_rule_on(p):
        return set()
    c = c or cache()
    P, C = c["profiles"], c["companies"]
    out = set()
    for r in judged:
        if not wanted(r, tabs) or r.get("_override") or {"Status", "Notes"} & set(r.get("_locked") or []):
            continue
        prof = P.get(pkey(r))
        if prof is None or prof.get("miss") == 1:  # not looked up yet, or its one retry is still to come
            out.add(str(r["Key"])); continue
        url = choose(prof, r).get("url")
        if url and (slug_of(url) or ("",))[0] == "company" and url not in C:
            out.add(str(r["Key"]))
    return out


def lookup(actor, urls, price, on_batch):
    """Run actor over urls in batches. -> '' when all ran, else why it stopped (the rest waits for the next run)."""
    name = actor.split("~")[1]
    for i in range(0, len(urls), BATCH):
        batch = urls[i:i + BATCH]
        cap = round(len(batch) * price * 1.25 + 0.01, 2)
        inp = ({"queries": batch, "profileScraperMode": "Profile details no email ($4 per 1k)"} if actor == PROFILE
               else {"companies": batch})
        try:
            run, items = apify.run(actor, inp, len(batch), cap)
        except RuntimeError as e:
            print(f"  {name} batch failed: {e}; the rest waits for the next run")
            return "failed"
        got = {}
        for it in items:
            q = it.get("originalQuery") or {}
            s = slug_of(q.get("query") or q.get("search") or it.get("linkedinUrl") or it.get("url") or "")
            if s:
                got[s[1]] = it
        print(f"  {name}: {len(batch)} asked, {len(items)} returned, ${max(apify.cost(run), len(items) * price):.3f}")
        on_batch(batch, got, run.get("status") == "SUCCEEDED" and bool(items))
    return ""


def apply_rule(judged, P, C, p=None):
    moved = 0
    for r in judged:
        if r.get("_tab") in ("Excluded", "dropped") or r.get("_override") or r.get("_manual"):
            continue
        prof = P.get(pkey(r)) if pkey(r) else None
        comp = C.get(choose(prof, r).get("url")) if prof else None
        rule, why = verdict(r, prof, comp, p)
        if not rule:
            continue
        if {"Status", "Notes"} & set(r.get("_locked") or []):
            print(f"  not moved, you edited its Status or Notes: {r.get('Name')} ({why})"); continue
        r["_tab"] = r["Type"] = "Excluded"
        note = f"excluded ({rule}): {why}"
        if note not in str(r.get("Notes") or ""):
            r["Notes"] = note + (f" | {r['Notes']}" if r.get("Notes") else "")
        moved += 1
    return moved


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lead.py enrich")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    with common.lock():
        run(a.dry_run)


def run(dry_run=False):
    p = common.profile()
    on, tabs = settings(p)
    judged, c = load(common.path("judged.json"), []), cache()
    P, C = c["profiles"], c["companies"]
    if not on:
        moved = apply_rule(judged, P, C, p)
        save(common.path("judged.json"), judged)
        print(f"enrichment is off; country rule moved {moved} leads to Excluded")
        return
    again = lambda k: k not in P or P[k].get("miss") == 1  # a profile missing from a good run is tried once more
    todo = sorted((r for r in judged if wanted(r, tabs) and again(pkey(r))), key=lambda r: -(r.get("_posted_ms") or 0))
    people = list(dict.fromkeys(f"https://www.linkedin.com/in/{slug_of(r['Profile'])[1]}" for r in todo
                                if slug_of(r["Profile"])[0] == "in"))
    if dry_run:
        print(f"{len(people)} profile lookups, up to {len(people)} company lookups; worst case "
              f"${2 * len(people) * FALLBACK_PRICE:.2f}")
        return
    price = {}
    for actor in (PROFILE, COMPANY):
        try:
            price[actor] = apify.prices(actor).get(EVENT[actor]) or FALLBACK_PRICE
        except RuntimeError:
            price[actor] = FALLBACK_PRICE
    left, _ = apify.spendable(None, p)
    recent = load(common.path("state.json"), {}).get("last_run_spend") or {}
    if recent.get("at") and now().timestamp() * 1000 - (common.ms_of(recent["at"]) or 0) < 6 * 3600_000:
        left = max(0.0, left - float(recent.get("usd") or 0))  # this run's search already used part of the budget
    room = int(left // (price[PROFILE] + price[COMPANY])) if people else 0
    if len(people) > room:
        print(f"the budget covers {room} of {len(people)} leads now; the rest wait for the next run")
        people = people[:room]
    for r in todo:  # a company page is its own company: no profile lookup
        s = slug_of(r["Profile"])
        if s[0] == "company":
            P[f"company:{s[1]}"] = {"positions": [{"company": r.get("Name", ""), "url": r["Profile"]}], "location": ""}

    def keep_profiles(batch, got, healthy):
        for url in batch:
            s = slug_of(url)[1]
            it = got.get(s)
            if not it and not healthy:
                continue  # a failed run: try again next time
            miss = 0 if it else (P.get(f"in:{s}") or {}).get("miss", 0) + 1
            it = it or {}
            loc = it.get("location")
            site = (it.get("websites") or [""])[0] if it.get("websites") else ""
            P[f"in:{s}"] = {"positions": positions(it), "website": site.get("url", "") if isinstance(site, dict) else site,
                            "location": (loc.get("linkedinText") if isinstance(loc, dict) else loc) or "",
                            "miss": miss, "at": now().isoformat(timespec="seconds")}
        save(common.path("enrich_cache.json"), c)

    def keep_companies(batch, got, healthy):
        for url in batch:
            it = got.get(slug_of(url)[1])
            if not it and not healthy:
                continue
            it = it or {}
            hq, hq_text = hq_of(it)
            ind = (it.get("industries") or [{}])[0] or {}
            C[url] = {"industry": (ind.get("name") if isinstance(ind, dict) else str(ind)) or "", "website": it.get("website") or "",
                      "size": it.get("employeeCount") or "", "hq": hq, "hq_text": hq_text, "miss": int(not it)}
        save(common.path("enrich_cache.json"), c)

    failed = lookup(PROFILE, people, price[PROFILE], keep_profiles) if people else ""
    curls = list(dict.fromkeys(u for r in judged if wanted(r, tabs) and pkey(r) in P
                               for u in [choose(P[pkey(r)], r).get("url")]
                               if u and (slug_of(u) or ("",))[0] == "company" and u not in C))
    room = int(max(0.0, left - len(people) * price[PROFILE]) // price[COMPANY])
    if len(curls) > room:
        print(f"the budget covers {room} of {len(curls)} company lookups now; the rest wait for the next run")
        curls = curls[:room]
    if curls and not failed:
        failed = lookup(COMPANY, curls, price[COMPANY], keep_companies)
    save(common.path("enrich_cache.json"), c)

    filled = 0
    for r in judged:
        prof = P.get(pkey(r)) if pkey(r) else None
        if not prof or r.get("_manual"):
            continue
        pick, locked = choose(prof, r), set(r.get("_locked") or [])
        comp = C.get(pick.get("url")) or {}
        if pick.get("company") and "Company" not in locked and (not r.get("Company") or r.get("_company_src") == "headline"):
            if r.get("Company") != pick["company"]:
                r["Company"] = pick["company"]; filled += 1
            r["_company_src"] = "profile"
        for col, val in (("Location", prof.get("location")), ("Website", comp.get("website") or prof.get("website")),
                         ("Industry", comp.get("industry")), ("Company size", comp.get("size"))):
            if val and not r.get(col) and col not in locked:
                r[col] = str(val); filled += 1
    moved = apply_rule(judged, P, C, p)
    save(common.path("judged.json"), judged)
    print(f"filled {filled} cells; country rule moved {moved} leads to Excluded; cache: {len(P)} people, {len(C)} companies"
          + ("; some lookups failed and will be retried" if failed else ""))
