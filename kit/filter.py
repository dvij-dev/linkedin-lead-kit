"""Turn new search results into leads and apply the client's rough rules, before the judge.

  python lead.py filter              # data/inbox/ -> queue.json (to judge) / rejected.jsonl (clear noise)
  python lead.py filter --rescore    # after changing the rules: re-check past rejects, requeue the ones that now pass

Rules live in config/profile.json "rules" and are written with the client at setup:
- reject: [{"pattern": "open to work", "why": "job seeker", "field": "text|headline|any"}]: clear noise, never judged
  (skim it: python lead.py judge rejected). A plain string is a pattern too. Patterns are case-insensitive regexes.
- review: same shape; a match sends the lead to the judge marked for the Review tab.
- must_match: [patterns]; a post that matches none of them is rejected (posts only, optional).
- company_pages: keep | review | reject: what to do with posts written by a company page (default review).
- exclude_countries / only_countries: a lead naming an excluded country, or located outside the countries the client
  serves, only gets a suggestion of Excluded; it is never rejected here. The judge decides, and enrichment checks the
  company's LinkedIn page later.
Anything uncertain is kept: the rules exist to save the judge time, not to make the call.
"""
import argparse, re
from collections import Counter
from kit import common
from kit.common import load, save, append_jsonl, read_jsonl, write_jsonl, now

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+\w")


def profile_url(author):
    if author.get("publicIdentifier"):
        return f"https://www.linkedin.com/in/{author['publicIdentifier']}"
    return re.sub(r"\?.*$", "", str(author.get("linkedinUrl") or ""))


def company_from_headline(h):
    """'Owner at Acme Cafe | ...' -> 'Acme Cafe'. Only a capitalised name after 'at' or '@' counts, so 'brilliant at
    what you do' is not a company."""
    for m in re.finditer(r"(?:\b[Aa]t\b|@)\s*([A-Z0-9&][^|,·•(.!?;:]{1,60})", str(h or "")):
        name = re.split(r"\s[-–—]\s|\s+(?:and|helping|where|who)\s", m.group(1))[0].strip()
        if name:
            return name
    return ""


def plain(t):
    """Curly quotes as straight ones, so a rule written with ' matches LinkedIn's ’."""
    return str(t or "").translate(str.maketrans("‘’“”", "''\"\""))


def place(v):
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return str(v.get("linkedinText") or v.get("text") or v.get("default") or
                   ", ".join(str(x) for x in (v.get("city"), v.get("country")) if x) or "")
    return ""


def normalize(it):
    """A post or a person from the inbox -> the lead shape the judge and the sheet use."""
    if it.get("_kind") == "person":
        pos = it.get("currentPositions") or it.get("currentPosition") or it.get("experience") or []
        pos = [x for x in pos if isinstance(x, dict)] if isinstance(pos, list) else []
        name = it.get("name") or f"{it.get('firstName') or ''} {it.get('lastName') or ''}".strip()
        slug = it["_key"].split(":", 1)[1]
        company = (pos[0].get("companyName") or "").strip() if pos else ""
        return {"id": it["_key"], "kind": "person", "url": "", "profile": f"https://www.linkedin.com/in/{slug}",
                "name": str(name), "headline": str(it.get("headline") or ""),
                "company": company or company_from_headline(it.get("headline")),
                "company_url": (pos[0].get("companyLinkedinUrl") or "") if pos else "",
                "location": place(it.get("location")), "text": str(it.get("about") or it.get("summary") or ""),
                "posted_ms": None, "found_at": it.get("_found_at", ""), "searches": list(it.get("_searches") or []),
                "author_type": "profile", "email": ""}
    a = it.get("author") if isinstance(it.get("author"), dict) else {}
    text = str(it.get("content") or "")
    pid = it["_key"]
    return {"id": pid, "kind": "post",
            "url": str(it.get("linkedinUrl") or f"https://www.linkedin.com/feed/update/urn:li:activity:{pid}/"),
            "profile": profile_url(a), "name": str(a.get("name") or ""), "headline": str(a.get("info") or ""),
            "company": company_from_headline(a.get("info")), "company_url": "", "location": place(a.get("location")),
            "text": text, "posted_ms": it.get("_posted_ms"), "found_at": it.get("_found_at", ""),
            "searches": list(it.get("_searches") or []), "author_type": str(a.get("type") or "profile"),
            "email": ", ".join(dict.fromkeys(m.group().rstrip(".") for m in EMAIL.finditer(text)))[:200]}


def _rules(r):
    out = []
    for x in r or []:
        pat, why, field = (x.get("pattern"), x.get("why") or x.get("pattern"), x.get("field")) if isinstance(x, dict) else (x, x, None)
        if not isinstance(pat, str) or not pat.strip():
            continue  # check reports it; a missing pattern must never become the regex 'None'
        out.append((re.compile(pat, re.I), str(why), field or "any"))
    return out


def _hit(rules, lead):
    fields = {"text": plain(lead["text"]), "headline": plain(lead["headline"]),
              "any": plain(f"{lead['headline']} \n {lead['text']}")}
    for rx, why, field in rules:
        if rx.search(fields.get(field, fields["any"])):
            return why
    return None


def score(lead, p=None):
    """-> {route: keep|reject, tab, reasons, flags}."""
    rules = common.rules(p)
    flags, reasons = [], []
    why = _hit(_rules(rules.get("reject")), lead)
    if why:
        return {"route": "reject", "tab": "", "reasons": [why], "flags": []}
    must = _rules(rules.get("must_match"))
    if lead["kind"] == "post" and must and not _hit(must, lead):
        return {"route": "reject", "tab": "", "reasons": ["none of the must_match words"], "flags": []}
    if lead["author_type"] == "company":
        mode = rules.get("company_pages", "review")
        if mode == "reject":
            return {"route": "reject", "tab": "", "reasons": ["written by a company page"], "flags": []}
        if mode == "review":
            flags.append("company_page"); reasons.append("written by a company page")
    why = _hit(_rules(rules.get("review")), lead)
    if why:
        flags.append("review_rule"); reasons.append(why)
    c = common.country_in(f"{lead['location']} | {lead['headline']} | {lead['company']} | {lead['text']}",
                          common.excluded_countries(p))
    far = common.outside(lead["location"], "", p) if common.only_countries(p) else ""
    if c:
        flags.append("country_hint"); reasons.append(f"mentions {c} (an excluded country): check whose company it is")
    elif far:
        flags.append("country_hint"); reasons.append(f"located {far} ({lead['location']}): check where the company is")
    tab = "Excluded" if c else "Review" if flags else ""
    return {"route": "keep", "tab": tab, "reasons": reasons or ["no rule matched"], "flags": flags}


def known_ids(queue):
    return ({q["id"] for q in queue} | {str(r["Key"]) for r in load(common.path("judged.json"), [])}
            | {str(r["id"]) for r in read_jsonl(common.path("rejected.jsonl"))})


def credit_judged(credits):
    """A search that found a known lead again joins its Search cell, so the client sees which searches work."""
    judged, n = load(common.path("judged.json"), []), 0
    for r in judged:
        more = sorted(credits.get(str(r["Key"]), set()) - set(r.get("_searches") or []))
        if more:
            if str(r.get("Search", "")) == ", ".join(r.get("_searches") or []):  # not edited by the client
                r["Search"] = ", ".join(list(r.get("_searches") or []) + more)
            r["_searches"] = list(r.get("_searches") or []) + more
            n += 1
    if n:
        save(common.path("judged.json"), judged)
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lead.py filter")
    ap.add_argument("--rescore", action="store_true")
    a = ap.parse_args(argv)
    with common.lock():
        rescore() if a.rescore else run()


def run():
    inbox = common.path("inbox")
    files = sorted(inbox.glob("*.json"))
    if not files:
        print("inbox empty: nothing new to filter"); return
    p = common.profile()
    batch = common.stamp()
    queue = load(common.path("queue.json"), [])
    known = known_ids(queue)
    routes, rejects, credits, added = Counter(), [], {}, 0
    for f in files:
        for it in load(f, []):
            if not isinstance(it, dict):
                continue
            if "_credit" in it:
                credits.setdefault(str(it["_credit"]), set()).update(it.get("_searches") or []); continue
            lead = normalize(it)
            if lead["id"] in known:
                credits.setdefault(lead["id"], set()).update(lead["searches"]); continue
            known.add(lead["id"])
            s = score(lead, p)
            routes[s["route"]] += 1
            if s["route"] == "reject":
                rejects.append({**lead, "reason": s["reasons"][0], "by": "rules", "batch": batch}); continue
            queue.append({**lead, "suggest": {k: s[k] for k in ("tab", "reasons", "flags")}}); added += 1
    for q in queue:  # a queued lead found again by another search
        q["searches"] = sorted(set(q["searches"]) | credits.pop(q["id"], set()))
    credited = credit_judged(credits) if credits else 0
    queue.sort(key=lambda q: -(q.get("posted_ms") or 0))
    save(common.path("queue.json"), queue)
    if rejects:
        st = load(common.path("state.json"), {})
        st.setdefault("unskimmed", []).append(batch)
        save(common.path("state.json"), st)
        append_jsonl(common.path("rejected.jsonl"), rejects)
    (inbox / "done").mkdir(parents=True, exist_ok=True)
    for f in files:  # only after the queue is on disk
        f.replace(inbox / "done" / f.name)
    print(f"kept {added} for the judge (queue now {len(queue)}); rejected {len(rejects)} as clear noise"
          + (" (skim them: python lead.py judge rejected)" if rejects else "")
          + (f"; {credited} sheet leads credited to more searches" if credited else ""))


def rescore():
    p = common.profile()
    queue = load(common.path("queue.json"), [])
    judged = {str(r["Key"]) for r in load(common.path("judged.json"), [])}
    queued = {q["id"] for q in queue}
    rows, back = read_jsonl(common.path("rejected.jsonl")), 0
    for i, r in enumerate(rows):
        if r.get("by") == "rules" and not r.get("requeued") and r["id"] not in judged | queued:
            s = score(r, p)
            if s["route"] == "keep":
                lead = {k: v for k, v in r.items() if k not in ("reason", "by", "batch", "requeued")}
                queue.append({**lead, "suggest": {k: s[k] for k in ("tab", "reasons", "flags")}})
                queued.add(r["id"]); rows[i] = dict(r, requeued=True); back += 1
    queue.sort(key=lambda q: -(q.get("posted_ms") or 0))
    save(common.path("queue.json"), queue)
    write_jsonl(common.path("rejected.jsonl"), rows)
    print(f"re-checked rule rejects: {back} now pass and were queued for the judge")
