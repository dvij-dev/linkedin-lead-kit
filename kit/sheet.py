"""Write judged leads into the client's Google Sheet: one tab per lead type, newest first, plus Review, Excluded, meta.

  python lead.py push              # read the sheet, keep the client's changes, enrich, then write
  python lead.py push --dry-run    # CSVs in data/out/, no Google calls
  python lead.py push --no-enrich  # skip enrichment this time

Auth: a Google service account key in config/google_key.json; the sheet (profile.json sheet_id) is shared with the
key's email as Editor.

The sheet is the client's working copy, and what they do in it sticks:
- A cell they edit (any column but Key and Type) is kept and never overwritten, enrichment included. The push tells
  their edits apart from its own by a snapshot of what it last wrote (data/sheet_written.json).
- A row they move to another of the kit's tabs stays there. Moving a row out of Excluded overrides the country rule
  for that lead for good.
- Status "drop" removes a row. Deleting the row does the same; if many rows of one tab vanish at once (more than half,
  and more than 3) the push stops instead, in case it was an accident (--accept-deletes when it was meant).
- Rows they add by hand are kept (keyed by the post or profile URL when there is one).
- Columns they add after Key are kept with their values and formulas.
- The same lead on two rows (a copy pasted into another tab) stops the push until one is deleted.
- Nothing is written if a header is damaged (no Key column, a header used twice, data under a blank header), or if a
  tab changes while the push runs (run it again).
Managed cells are written as plain text, so a post can never inject a formula. Tabs the client creates themselves are
never read or written.
"""
import argparse, csv, hashlib, re, sys
from kit import common, enrich
from kit.common import load, save, now

COLS = ["Found", "Posted", "Type", "Source", "Name", "Headline", "Company", "Location", "Profile", "Post", "Post text",
        "Email", "Website", "Industry", "Company size", "Search", "Status", "Notes", "Key"]
FIXED = ("Key", "Type")
META = "meta"


class Stop(SystemExit):
    pass


def stop(msg):
    raise Stop("nothing written to the sheet: " + msg)


def derive_key(post, profile, name):
    """The key of a row the client typed in: the post id, else the person's LinkedIn slug, else a hash."""
    m = re.search(r"(?:activity|ugcPost|share)[:\-](\d{16,20})", str(post or ""))
    if m:
        return m.group(1)
    s = common.slug_of(profile)
    if s:
        return f"person:{s[1]}" if s[0] == "in" else f"company:{s[1]}"
    return "manual-" + hashlib.sha1(f"{name}|{profile}|{post}".encode()).hexdigest()[:10]


def cell(r, c):
    v = r.get(c, "")
    return "" if v is None else str(v)


A1_REF = re.compile(r"\"(?:[^\"]|\"\")*\"|'(?:[^']|'')*'|(?<![\w.$])(\$?[A-Z]{1,3})(\$?)(\d+)(?![\w(!])")


def moved_formula(v, delta):
    """A formula from the client's own column, its row references shifted with its row (A5 -> A7; $A$5 stays), as a
    sheet does when rows are sorted. Text inside quotes is left alone."""
    if not delta or not isinstance(v, str) or not v.startswith("="):
        return v
    return A1_REF.sub(lambda m: m.group(0) if not m.group(1) or m.group(2) else
                      f"{m.group(1)}{max(1, int(m.group(3)) + delta)}", v)


def fold(judged, book, written, accept_deletes=False, prior=None):
    """Read the kit's tabs and fold the client's changes into judged (in place). prior: {Key: tab} seen by an earlier
    fold in the same push, so a move it already took is not taken again over a later pipeline move.
    -> (reads, extra headers, counts, {Key: tab})."""
    by_key = {str(r["Key"]): r for r in judged}
    present = set(book.tabs())
    reads, extras, seen = {}, {}, {}
    n = {"edits": 0, "moved": 0, "added": 0, "dropped": 0}
    for tab in common.all_tabs():
        if tab not in present:
            continue
        values = book.read(tab)
        reads[tab] = values
        if not values:
            continue
        header = [str(h).strip() for h in values[0]]
        named = [h for h in header if h]
        if "Key" not in header:
            stop(f"tab {tab!r} has no Key column header (put it back, or undo the change)")
        if len(named) != len(set(named)):
            stop(f"tab {tab!r} has a header used twice: {sorted({h for h in named if named.count(h) > 1})}")
        idx = {h: i for i, h in enumerate(header) if h}
        blank_cols = [i for i, h in enumerate(header) if not h]
        extras[tab] = [h for h in header if h and h not in COLS]
        for row_no, raw in enumerate(values[1:], start=2):
            if not any(str(x).strip() for x in raw):
                continue
            if any(i < len(raw) and str(raw[i]).strip() for i in blank_cols):
                stop(f"tab {tab!r} row {row_no} has data under a blank header: give that column a name")
            get = lambda h: None if h not in idx else (str(raw[idx[h]]) if idx[h] < len(raw) else "")
            key = (get("Key") or "").strip()
            if not key:
                if not any((get(c) or "").strip() for c in COLS if c not in FIXED):
                    stop(f"tab {tab!r} row {row_no} has values only in your own columns; fill Name (or delete the row)")
                key = derive_key(get("Post"), get("Profile"), get("Name") or "|".join(get(c) or "" for c in COLS))
            if key in seen:
                stop(f"the same lead ({key}) is on two rows: {seen[key]} and {tab} row {row_no}. Delete one, then push again")
            seen[key] = f"{tab} row {row_no}"
            r, w = by_key.get(key), written.get(key)
            if r is None:
                r = {"Key": key, "_tab": tab, "_manual": True, "_found_at": now().isoformat(timespec="seconds")}
                for c in COLS:
                    if c not in FIXED and get(c) is not None:
                        r[c] = get(c)
                r["_locked"] = [c for c in COLS if c not in FIXED and r.get(c)]
                judged.append(r); by_key[key] = r; n["added"] += 1
            else:
                last = (w or {}).get("tab", r["_tab"])
                if tab != last and (prior or {}).get(key) != tab:
                    r["_tab"] = tab; n["moved"] += 1
                    if last == "Excluded":
                        r["_override"] = True
                if w:
                    for c in COLS:
                        val = get(c)
                        if c in FIXED or val is None:
                            continue
                        if val == str(w["cells"].get(c, "")):
                            if c in (r.get("_locked") or []):
                                r[c] = val  # a cell the client edited once is theirs: the sheet's value always wins
                            continue
                        r[c] = val
                        r["_locked"] = sorted(set(r.get("_locked") or []) | {c})
                        n["edits"] += not (c == "Status" and val.strip().lower() == "drop")  # counted as a drop
            if str(r.get("Status", "")).strip().lower() == "drop":
                r["_tab"] = "dropped"; n["dropped"] += 1
            r["_extra"] = {h: (raw[idx[h]] if idx[h] < len(raw) else "") for h in extras[tab]}
            r["_row"] = row_no  # where its formulas were written, so they can follow the row
    # a row missing from the tab it was last written to was deleted by the client, unless the kit itself moved it since
    # (a push that failed part way leaves such a lead off the sheet until the next write)
    gone = [k for k, w in written.items() if k not in seen and w.get("tab") in reads and k in by_key
            and by_key[k]["_tab"] == w.get("tab")]
    for tab in {written[k]["tab"] for k in gone}:
        lost = sum(written[k]["tab"] == tab for k in gone)
        had = sum(w.get("tab") == tab for w in written.values())
        if lost > max(3, had // 2) and not accept_deletes:
            stop(f"{lost} of {had} rows vanished from tab {tab!r} at once. If you meant to delete them, push with "
                 f"--accept-deletes; if not, undo in the sheet (File, Version history) and push again")
    for k in gone:
        by_key[k]["_tab"] = "dropped"; n["dropped"] += 1
    return reads, extras, n, {k: v.split(" row ")[0] for k, v in seen.items()}


def layout(judged, held):
    by = {t: [] for t in common.all_tabs()}
    for r in judged:
        t = r.get("_tab")
        if t == "dropped" or str(r["Key"]) in held:
            continue
        by.get(t, by["Review"]).append(r)
    for rows in by.values():
        rows.sort(key=lambda r: (str(r.get("_found_at") or ""), r.get("_posted_ms") or 0), reverse=True)
    return by


def write(judged, book, reads, extras, held, snap):
    """Write every kit tab, filling snap {Key: {tab, cells}} tab by tab, so a failure part way still records what
    reached the sheet."""
    by = layout(judged, held)
    present = set(book.tabs())
    for tab, rows in by.items():
        if tab not in present:
            book.add_tab(tab)
        elif book.read(tab) != reads.get(tab, []):
            stop(f"tab {tab!r} changed while the push was running; run the push again")
        heads = list(extras.get(tab, []))
        for r in rows:  # columns of rows that moved in from another tab
            heads += [h for h in (r.get("_extra") or {}) if h not in heads]
        managed = [COLS] + [[cell(r, c) if c != "Type" else tab for c in COLS] for r in rows]
        extra = [heads] + [[moved_formula((r.get("_extra") or {}).get(h, ""), i + 2 - (r.get("_row") or i + 2)) for h in heads]
                           for i, r in enumerate(rows)] if heads else []
        book.write(tab, managed, extra, reads.get(tab, []))
        for r in rows:
            snap[str(r["Key"])] = {"tab": tab, "cells": {c: (tab if c == "Type" else cell(r, c)) for c in COLS}}


def meta_rows(judged):
    st = load(common.path("state.json"), {})
    u = st.get("apify_usage") or {}
    when = lambda s: common.local(common.ms_of(s)) if s and common.ms_of(s) else ""
    rows = [["key", "value"], ["last push", now().astimezone(common.tz()).strftime("%Y-%m-%d %H:%M")],
            ["last search", when(st.get("last_search"))],
            ["Apify used this cycle", f"${u.get('used', 0):.2f} of ${u.get('limit', 0):.2f} ({u.get('start', '?')} to {u.get('end', '?')})" if u else ""],
            ["waiting to judge", str(len(load(common.path("queue.json"), [])))]]
    tabs = {}
    for r in judged:
        tabs[r.get("_tab")] = tabs.get(r.get("_tab"), 0) + 1
    rows += [[f"leads in {t}", str(tabs.get(t, 0))] for t in common.all_tabs()]
    for sid, runs in sorted((st.get("runs") or {}).items()):
        kept = sum(sid in (r.get("_searches") or []) and r.get("_tab") in common.lead_tabs() for r in judged)
        rows.append([f"search {sid}", f"{len(runs)} runs, {sum(x.get('returned', 0) for x in runs)} results, "
                                      f"{kept} leads kept, ${sum(x.get('cost', 0) for x in runs):.2f}"])
    return rows


class GBook:
    """The Google Sheet through gspread."""

    def __init__(self):
        import gspread
        key = common.CONFIG / "google_key.json"
        if not key.exists():
            raise SystemExit("config/google_key.json is missing: see SETUP.md, step Google Sheet")
        self.gc = gspread.service_account(filename=str(key))
        self.sh = self.gc.open_by_key(str(common.profile()["sheet_id"]).strip())
        self._ws = {ws.title: ws for ws in self.sh.worksheets()}

    def tabs(self):
        return list(self._ws)

    def read(self, tab):  # formulas as formulas, dates as the text the client sees
        return self._ws[tab].get_values(value_render_option="FORMULA", date_time_render_option="FORMATTED_STRING")

    def add_tab(self, tab):
        ws = self.sh.add_worksheet(title=tab, rows=200, cols=len(COLS) + 5)
        ws.freeze(rows=1)
        self._ws[tab] = ws

    def write(self, tab, managed, extra, old):
        from gspread.utils import rowcol_to_a1
        ws = self._ws[tab]
        width = len(COLS) + (len(extra[0]) if extra else 0)
        rows = len(managed)
        if ws.row_count < rows + 1:
            ws.add_rows(rows + 50 - ws.row_count)
        if ws.col_count < width:
            ws.add_cols(width - ws.col_count)
        # one call, so the client's columns can never land beside other leads: the kit's cells go in as text (a
        # leading ' keeps '=...' or '+44...' from becoming a formula or a number), the client's as they typed them
        text = lambda v: f"'{v}" if v != "" else ""
        rows_out = [[text(v) for v in m] + (list(extra[i]) if extra else []) for i, m in enumerate(managed)]
        ws.update(values=rows_out, range_name="A1", value_input_option="USER_ENTERED")
        old_rows, old_cols = len(old), max((len(x) for x in old), default=0)
        if old_rows > rows:
            ws.batch_clear([f"A{rows + 1}:{rowcol_to_a1(old_rows, max(width, old_cols)).rstrip('0123456789')}{old_rows}"])

    def write_meta(self, rows):
        if META not in self._ws:
            self._ws[META] = self.sh.add_worksheet(title=META, rows=100, cols=3)
        ws = self._ws[META]
        ws.clear()
        ws.update(values=rows, range_name="A1", value_input_option="RAW")
        blank = self._ws.get("Sheet1")  # a new Google Sheet's empty default tab, once the kit's tabs exist
        if blank is not None and not any(str(c).strip() for r in blank.get_values() for c in r):
            self.sh.del_worksheet(blank)
            del self._ws["Sheet1"]


def dry_run(judged):
    out = common.path("out")
    out.mkdir(parents=True, exist_ok=True)
    held = enrich.pending(judged)
    for tab, rows in layout(judged, held).items():
        with open(out / f"{tab}.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(COLS)
            safe = lambda v: f"'{v}" if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v  # Excel would run it
            w.writerows([[safe(cell(r, c)) for c in COLS] for r in rows])
        print(f"  {tab}: {len(rows)} rows -> data/out/{tab}.csv")
    if held:
        print(f"  {len(held)} leads held off until their country check")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lead.py push")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-enrich", action="store_true")
    ap.add_argument("--accept-deletes", action="store_true")
    a = ap.parse_args(argv)
    with common.lock():
        judged = load(common.path("judged.json"), [])
        if a.dry_run:
            return dry_run(judged)
        book = GBook()
        written = load(common.path("sheet_written.json"), {})
        _, _, n, first = fold(judged, book, written, a.accept_deletes)  # drops and moves first: enrichment never pays for them
        save(common.path("judged.json"), judged)
    if not a.no_enrich:
        try:
            with common.lock():
                enrich.run()
        except (RuntimeError, SystemExit) as e:
            if isinstance(e, Stop):
                raise
            print(f"enrichment failed ({e}); pushing without it")
    with common.lock():
        judged = load(common.path("judged.json"), [])
        book = GBook()  # a fresh read: edits made while enrichment ran are kept too
        reads, extras, _, _ = fold(judged, book, written, a.accept_deletes, first)  # counted once, from the first read
        held = enrich.pending(judged) - set(written)  # a lead already on the sheet stays on it
        snap = {}
        try:
            write(judged, book, reads, extras, held, snap)
        except BaseException:
            save(common.path("sheet_written.json"), {**written, **snap})  # the tabs that did reach the sheet
            save(common.path("judged.json"), judged)
            raise
        save(common.path("judged.json"), judged)
        save(common.path("sheet_written.json"), snap)
        book.write_meta(meta_rows(judged))
    shown = len(snap)
    print(f"sheet updated: {shown} leads on the sheet; your changes kept: {n['edits']} edits, {n['moved']} moves, "
          f"{n['added']} rows you added, {n['dropped']} dropped" + (f"; {len(held)} held until their country check" if held else ""))


def check():
    """Open the sheet and report, writing nothing."""
    import json
    key = common.CONFIG / "google_key.json"
    email = json.loads(key.read_text(encoding="utf-8")).get("client_email") if key.exists() else None
    try:
        b = GBook()
    except SystemExit:
        raise
    except Exception as e:
        hint = f" Share the sheet with {email} as Editor." if email else ""
        raise SystemExit(f"cannot open the sheet: {type(e).__name__}: {str(e)[:200]}.{hint}") from None
    return b.sh.title, email, b.tabs()
