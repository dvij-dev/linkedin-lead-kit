"""LinkedIn lead kit: one command for every step. Claude runs these; see OPERATOR.md for the order.

  python lead.py check                 # settings, Apify key, live prices, Google Sheet access (spends nothing)
  python lead.py fields posts|people   # every filter the search accepts, with allowed values (free)
  python lead.py search [--dry-run]    # run the searches, then the rough filter (spends Apify credit)
  python lead.py search --test ID      # a small sample to test keywords
  python lead.py judge show|apply|rejected|requeue
  python lead.py push [--dry-run]      # keep the client's sheet edits, enrich, write the sheet
  python lead.py status                # where things stand (free)
  python lead.py filter | enrich       # single steps (search and push already run them)
"""
import json, sys
from kit import common


def check():
    ok = True
    try:
        p = common.profile()
    except SystemExit as e:
        print(f"settings: {e}"); return 1
    probs = common.check_profile(p)
    print("settings: " + ("ok" if not probs else "PROBLEMS\n  " + "\n  ".join(probs)))
    ok &= not probs
    from kit import search, apify, sheet
    try:
        search.PEOPLE_FIELDS = apify.input_schema(search.PEOPLE) if any(s.get("type") == "people" for s in common.searches()) else None
    except RuntimeError:
        search.PEOPLE_FIELDS = None
    sp = [x for s in common.searches() for x in search.check_search(s)]
    act = [s["id"] for s in common.searches() if s.get("active", True)]
    print(f"searches: {len(act)} active" + ("" if not sp else " PROBLEMS\n  " + "\n  ".join(sp)))
    ok &= not sp and bool(act)
    try:
        left, u = apify.spendable(None, p)
        print(f"Apify: key works; ${u['used']:.2f} used of ${u['limit']:.2f} this cycle; ${left:.2f} spendable per run now")
        for actor in (search.POSTS, search.PEOPLE):
            pr = apify.prices(actor)
            show = {k: v for k, v in pr.items() if k in ("post", "search-page")}
            print(f"  live price {actor.split('~')[1]}: {show}")
    except (RuntimeError, SystemExit) as e:
        print(f"Apify: PROBLEM {e}"); ok = False
    try:
        title, email, tabs = sheet.check()
        print(f"Google Sheet: opened '{title}' as {email}; tabs now: {tabs}")
    except SystemExit as e:
        print(f"Google Sheet: PROBLEM {e}"); ok = False
    print("READY" if ok else "NOT READY: fix the problems above")
    return 0 if ok else 1


def fields(kind):
    from kit import apify, search
    actor = {"posts": search.POSTS, "people": search.PEOPLE}.get(kind)
    if not actor:
        sys.exit("usage: python lead.py fields posts|people")
    skip = {"profileScraperMode", "maxItems", "startPage", "takePages", "maxPosts", "searchQueries", "mongoDbConnectionString",
            "mongoDbDatabaseName", "profileDeduplicationMode", "postFilteringMongoDbQuery", "postFilteringMongoDbAggregation"}
    for k, v in apify.input_schema(actor).items():
        if k in skip:
            continue
        items = v.get("items") or {}
        enum = v.get("enum") or items.get("enum")
        titles = v.get("enumTitles") or items.get("enumTitles")
        if enum and titles and len(titles) == len(enum):
            enum = [f"{e} = {t}" for e, t in zip(enum, titles)]
        print(f"{k} ({v.get('type')}): {common.clean(v.get('description') or v.get('title'))[:220]}"
              + (f"\n    allowed: {enum}" if enum else ""))


def status():
    st = common.load(common.path("state.json"), {})
    judged = common.load(common.path("judged.json"), [])
    tabs = {}
    for r in judged:
        tabs[r.get("_tab")] = tabs.get(r.get("_tab"), 0) + 1
    print(json.dumps({"last_search": st.get("last_search"), "waiting_for_budget": (st.get("waiting") or {}).get("ids", []),
                      "to_judge": len(common.load(common.path("queue.json"), [])),
                      "inbox_files": len(list(common.path("inbox").glob("*.json"))),
                      "unskimmed_reject_batches": st.get("unskimmed", []),
                      "leads_by_tab": tabs, "apify_usage": st.get("apify_usage")}, indent=1))


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__); return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "check":
        return check()
    if cmd == "fields":
        return fields(rest[0] if rest else "")
    if cmd == "status":
        return status()
    if cmd == "search":
        from kit import search, filter as flt
        try:
            search.main(rest)
        finally:
            if "--dry-run" not in rest and "--restart" not in " ".join(rest):
                flt.main([])  # results already paid for always reach the queue
        return 0
    mods = {"filter": "filter", "judge": "judge", "enrich": "enrich", "push": "sheet"}
    if cmd not in mods:
        sys.exit(f"unknown command {cmd!r}; python lead.py help")
    import importlib
    importlib.import_module(f"kit.{mods[cmd]}").main(rest)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) or 0)
