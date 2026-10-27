"""Shared paths, the client's settings and small helpers. Every other module reads settings through here.

Paths are read at call time (common.DATA / "x"), so the tests can point DATA and CONFIG at scratch folders.
"""
import contextlib, json, os, re, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

ROOT = Path(__file__).resolve().parent.parent
CONFIG = Path(os.environ.get("LEADKIT_CONFIG") or ROOT / "config")
DATA = Path(os.environ.get("LEADKIT_DATA") or ROOT / "data")

WINDOW_DAYS = 14          # a post search never looks further back than this
FIXED_TABS = ("Review", "Excluded")  # always there, after the client's own lead tabs
RESERVED = {"meta", "dropped"}


def path(name):
    return DATA / name


# ---------- settings ----------

def profile():
    p = load(CONFIG / "profile.json", None)
    if p is None:
        raise SystemExit("config/profile.json is missing: the setup has not been done yet (see SETUP.md)")
    return p


def searches():
    return load(CONFIG / "searches.json", [])


def lead_tabs(p=None):
    """The client's own lead tabs, in their order."""
    return [t["name"] for t in (p or profile()).get("tabs", [])]


def all_tabs(p=None):
    return lead_tabs(p) + list(FIXED_TABS)


def rules(p=None):
    return (p or profile()).get("rules") or {}


def excluded_countries(p=None):
    """[{name, code, aliases}] the client excludes, [] when the rule is off."""
    return [c for c in rules(p).get("exclude_countries") or [] if isinstance(c, dict) and c.get("name")]


def only_countries(p=None):
    """[{name, code, aliases}] the client serves, [] when any country is fine."""
    return [c for c in rules(p).get("only_countries") or [] if isinstance(c, dict) and c.get("name")]


def country_rule_on(p=None):
    return bool(excluded_countries(p) or only_countries(p))


def outside(text="", code="", p=None):
    """Why a place breaks the client's country rule ('in India', 'not in United Kingdom'), else ''. A blank place
    breaks nothing: unknown is not evidence."""
    ex, only = excluded_countries(p), only_countries(p)
    hit = country_by_code(code, ex) or (country_in(text, ex) if text else "")
    if hit:
        return f"in {hit}"
    if only and (code or str(text or "").strip()):
        fits = country_by_code(code, only) if code else country_in(text, only)
        if not fits:
            return f"not in {' or '.join(c['name'] for c in only)}"
    return ""


def tz(p=None):
    name = (p or profile()).get("timezone") or "UTC"
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # unknown name, or no tz database (pip install tzdata)
        print(f"warning: time zone {name!r} unavailable; using UTC", file=sys.stderr)
        return timezone.utc


def check_profile(p):
    """Problems with profile.json, [] when it is usable."""
    probs = []
    tabs = p.get("tabs")
    if not isinstance(tabs, list) or not tabs:
        probs.append("tabs: give at least one lead tab, e.g. [{\"name\": \"Good\", \"means\": \"...\"}]")
    else:
        names = [t.get("name") if isinstance(t, dict) else None for t in tabs]
        for n in names:
            if not n or not isinstance(n, str):
                probs.append("tabs: every tab needs a name and a 'means' line")
            elif n in FIXED_TABS or n.lower() in RESERVED:
                probs.append(f"tabs: {n!r} is built in; pick another name")
        if len(set(names)) != len(names):
            probs.append("tabs: two tabs share a name")
    if not str(p.get("sheet_id") or "").strip():
        probs.append("sheet_id: the Google Sheet id (the long part of its URL) is missing")
    for k in ("exclude_countries", "only_countries"):
        for c in rules(p).get(k) or []:
            if not isinstance(c, dict) or not c.get("name") or not re.fullmatch(r"[A-Za-z]{2}", str(c.get("code") or "")):
                probs.append(f"rules.{k}: each entry is {{\"name\": \"India\", \"code\": \"IN\", \"aliases\": [...]}}")
    basis = rules(p).get("country_basis", "company")
    if basis not in ("company", "poster", "either"):
        probs.append("rules.country_basis must be company, poster or either")
    for k in ("reject", "review", "must_match"):
        for x in rules(p).get(k) or []:
            pat = x.get("pattern") if isinstance(x, dict) else x
            if not isinstance(pat, str) or not pat.strip():
                probs.append(f"rules.{k}: every rule needs a non-empty pattern (got {str(x)[:60]})"); continue
            extra = set(x) - {"pattern", "why", "field"} if isinstance(x, dict) else set()
            if extra:
                probs.append(f"rules.{k}: unknown keys {sorted(extra)} in {str(x)[:60]}")
            if isinstance(x, dict) and x.get("field", "any") not in ("text", "headline", "any"):
                probs.append(f"rules.{k}: field must be text, headline or any")
            try:
                re.compile(str(pat), re.I)
            except re.error as e:
                probs.append(f"rules.{k}: bad pattern {pat!r} ({e})")
    return probs


# ---------- files ----------

def load(p, default):
    try:
        text = Path(p).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return default
    try:
        return json.loads(text)
    except ValueError as e:
        raise SystemExit(f"{p} is damaged, not JSON ({e}); fix or restore it") from None


def _replace(tmp, target):
    for i in range(6):  # Windows: antivirus or an editor can hold the target open briefly
        try:
            return os.replace(tmp, target)
        except PermissionError:
            if i == 5:
                raise
            time.sleep(0.3)


_SURROGATE = re.compile("[\ud800-\udfff]")


def _write(tmp, text):
    with open(tmp, "w", encoding="utf-8") as f:  # on disk before the rename: a power cut leaves old or new, never empty
        f.write(_SURROGATE.sub("�", text))
        f.flush()
        os.fsync(f.fileno())


def save(p, obj):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    _write(tmp, json.dumps(obj, indent=1, ensure_ascii=False))
    _replace(tmp, p)


def append_jsonl(p, rows):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(_SURROGATE.sub("�", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)))


def write_jsonl(p, rows):
    p = Path(p)
    tmp = p.with_suffix(p.suffix + ".tmp")
    _write(tmp, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    _replace(tmp, p)


def read_jsonl(p):
    if not Path(p).exists():
        return []
    rows = []
    with open(p, encoding="utf-8-sig", errors="replace") as f:
        for i, line in enumerate(f, 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    print(f"warning: {Path(p).name} line {i} is damaged (skipped)", file=sys.stderr)
    return rows


def secret(name):
    """A one-line value from config/ (the Apify token). Never echoes it."""
    p = CONFIG / name
    b = p.read_bytes() if p.exists() else b""
    try:
        parts = b.decode("utf-16" if b[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig").split()
    except UnicodeDecodeError:
        parts = []
    if len(parts) != 1:
        raise SystemExit(f"config/{name} " + ("is missing or empty" if not parts else "must hold only the value, on one line"))
    if not re.fullmatch(r"[A-Za-z0-9_-]+", parts[0]):
        raise SystemExit(f"config/{name} has a stray character (a smart quote or invisible character from a paste?): re-copy it")
    return parts[0]


@contextlib.contextmanager
def lock():
    """One step at a time. The OS drops the lock if the process dies."""
    DATA.mkdir(parents=True, exist_ok=True)
    f = open(DATA / ".lock", "a+")
    try:
        try:
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except ImportError:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise SystemExit("another step is running (data/.lock); wait for it to finish")
    try:
        yield
    finally:
        f.close()


# ---------- time ----------

def now():
    return datetime.now(timezone.utc)


def stamp():
    return now().strftime("%Y%m%d_%H%M%S")


def local(ms):
    """A time in ms for people: the client's time zone, minute precision."""
    return datetime.fromtimestamp(ms / 1000, tz()).strftime("%Y-%m-%d %H:%M") if ms else ""


def ms_of(iso_text):
    try:
        d = datetime.fromisoformat(str(iso_text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return int((d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp() * 1000)


def activity_ms(post_id):
    """LinkedIn activity/ugcPost/share ids carry their own time: id >> 22 = ms since epoch."""
    m = re.search(r"\d{16,20}", str(post_id or ""))
    return int(m.group()) >> 22 if m else None


def post_ms(item):
    """When a post was published: from its id (exact), else postedAt."""
    pa = item.get("postedAt") if isinstance(item.get("postedAt"), dict) else {}
    ms = activity_ms(item.get("id")) or activity_ms(item.get("linkedinUrl"))
    for v in (pa.get("timestamp"), pa.get("date")):
        ms = ms or (int(v) if str(v or "").isdecimal() else ms_of(v) if v else None)
    return ms


def cutoff_ms(days=WINDOW_DAYS):
    return int((now() - timedelta(days=days)).timestamp() * 1000)


# ---------- text ----------

def clean(s):
    """Untrusted text on one line: no control characters, line breaks or fence markers."""
    s = re.sub(r"<{3,}|>{3,}", lambda m: " ".join(m.group()), str(s or ""))
    return re.sub(r"[\x00-\x1f\x7f\x85  ]+", " ", s)


def country_in(text, countries=None):
    """Name of an excluded country that a place, company name or post names ('Pune, Maharashtra, India' -> 'India'),
    else ''. Country names count before aliases (cities), so 'Hyderabad, Pakistan' is Pakistan; the last named wins."""
    t = str(text or "").lower()
    countries = excluded_countries() if countries is None else countries
    for words in (lambda c: [c["name"]], lambda c: c.get("aliases") or []):
        hits = [(m.start(), c["name"]) for c in countries for w in words(c) if str(w).strip()
                for m in re.finditer(rf"(?<!\w){re.escape(str(w).lower().strip())}(?!\w)", t)]
        if hits:
            return max(hits)[1]
    return ""


def country_by_code(code, countries=None):
    code = str(code or "").upper()
    countries = excluded_countries() if countries is None else countries
    return next((c["name"] for c in countries if code and str(c.get("code") or "").upper() == code), "")


_OPS = re.compile(r"\b(AND|OR|NOT)\b")


def validate_query(q):
    """Problems with a LinkedIn boolean post query (HarvestAPI limits: 5 operators, 500 characters)."""
    problems = []
    if not str(q or "").strip():
        return ["empty query"]
    if any(c in q for c in "“”‘’"):
        problems.append("curly quotes (use straight \" quotes)")
    if q.count('"') % 2:
        problems.append("unbalanced quotes")
    if q.count("(") != q.count(")"):
        problems.append("unbalanced parentheses")
    bare = re.sub(r'"[^"]*"', "", q)
    ops = len(_OPS.findall(bare))
    if ops > 5:
        problems.append(f"{ops} operators (max 5)")
    if re.search(r"\b(and|or|not)\b", bare):
        problems.append("lowercase operator (write AND, OR, NOT)")
    if len(q) > 500:
        problems.append(f"{len(q)} characters (max 500)")
    return problems


def slug_of(url):
    """('in' | 'company', slug) for a LinkedIn profile or company URL, else None."""
    m = re.search(r"linkedin\.com/(in|company)/([^/?#]+)", str(url or ""))
    return (m.group(1), m.group(2).lower()) if m else None
