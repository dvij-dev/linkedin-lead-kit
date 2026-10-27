"""Small Apify REST v2 client (urllib, no SDK).

Every failure becomes RuntimeError with the HTTP status in the text ("-> 402"). Reads are retried; starting a run is
retried only when the request provably never reached Apify, so a flaky network never starts (and bills) a second run.
Each run carries a charge cap (maxTotalChargeUsd), so a run can never cost more than its cap. A started run that stops
being followed (Ctrl+C, an unreadable dataset) is recorded in data/orphan_runs.jsonl and aborted. Error text never
carries the token.
"""
import contextlib, http.client, json, re, sys, time, urllib.error, urllib.parse, urllib.request
from kit import common

BASE = "https://api.apify.com/v2"
DONE = {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}
RUN_TIMEOUT_S = 900

_token = None


def _scrub(msg):
    msg = re.sub(r"Bearer\s*[^\s'\"]*", "Bearer <token>", msg)
    return msg.replace(_token, "<token>") if _token else msg


def call(m, p, b=None, params=None, tries=4, auth=True, safe=None):
    """safe: may this request be repeated after an unclear failure? Default: only reads."""
    global _token
    safe = (m == "GET") if safe is None else safe
    url = BASE + p + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = {"Content-Type": "application/json", "User-Agent": "linkedin-lead-kit/1"}
    if auth:
        _token = _token or common.secret("apify_token.txt")
        headers["Authorization"] = f"Bearer {_token}"
    for i in range(tries):
        last = i == tries - 1
        req = urllib.request.Request(url, data=json.dumps(b).encode() if b is not None else None, method=m, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                t = r.read().decode()
            return json.loads(t) if t else {}
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            if (e.code == 429 or (safe and e.code >= 500)) and not last:
                time.sleep(5 * (i + 1)); continue
            raise RuntimeError(_scrub(f"{m} {p} -> {e.code}: {body}")) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
            unsent = isinstance(e, urllib.error.URLError) and not isinstance(e.reason, TimeoutError)
            if (safe or unsent) and not last:
                time.sleep(5 * (i + 1)); continue
            raise RuntimeError(_scrub(f"{m} {p} -> {e!r}")) from None


def actor_info(actor):
    """The actor's public record (no token): notice, pricing, stats."""
    return call("GET", f"/acts/{actor}", auth=False).get("data", {})


def input_schema(actor):
    """The actor's input fields from its latest build: {field: {type, description, enum, ...}}."""
    info = actor_info(actor)
    build = ((info.get("taggedBuilds") or {}).get("latest") or {}).get("buildId")
    if not build:
        return {}
    d = call("GET", f"/actor-builds/{build}", auth=False).get("data", {})
    s = d.get("inputSchema") or (d.get("actorDefinition") or {}).get("input") or {}
    s = json.loads(s) if isinstance(s, str) else s
    return s.get("properties") or {}


def prices(actor, info=None):
    """{event: USD} at the FREE-plan price (the highest tier: a safe worst case), plus '_min_cap' for the smallest charge
    cap the actor accepts."""
    info = info if info is not None else actor_info(actor)
    pi = (info.get("pricingInfos") or [{}])[-1]
    out = {"_min_cap": float(pi.get("minimalMaxTotalChargeUsd") or 0)}
    for ev, v in ((pi.get("pricingPerEvent") or {}).get("actorChargeEvents") or {}).items():
        price = v.get("eventPriceUsd")
        if price is None:
            tiers = v.get("eventTieredPricingUsd") or {}
            price = max((float((t or {}).get("tieredEventPriceUsd") or 0) for t in tiers.values()), default=None)
        if price is not None:
            out[ev] = float(price)
    return out


def usage():
    """Real spend this billing cycle and the account's own monthly limit."""
    d = call("GET", "/users/me/limits")["data"]
    cyc = d.get("monthlyUsageCycle") or {}
    return {"used": float(d["current"]["monthlyUsageUsd"]), "limit": float(d["limits"]["maxMonthlyUsageUsd"]),
            "start": (cyc.get("startAt") or "")[:10], "end": (cyc.get("endAt") or "")[:10]}


def spendable(u=None, p=None):
    """How much this run may spend: the account's credit left, within the client's budget settings
    (budget.monthly_usd for the cycle, budget.per_run_usd for one run). -> (usd, usage)."""
    u = u or usage()
    b = (p or common.profile()).get("budget") or {}
    left = max(0.0, u["limit"] - u["used"])
    if b.get("monthly_usd") is not None:
        left = min(left, max(0.0, float(b["monthly_usd"]) - u["used"]))
    if b.get("per_run_usd") is not None:
        left = min(left, float(b["per_run_usd"]))
    return left, u


def start(actor, run_input, max_items, cap_usd):
    params = {"maxItems": max_items, "maxTotalChargeUsd": f"{cap_usd:.2f}", "timeout": RUN_TIMEOUT_S}
    return call("POST", f"/acts/{actor}/runs", run_input, params, safe=False)["data"]


def wait(run_id, timeout_s=RUN_TIMEOUT_S + 120):
    t0, r = time.time(), None
    while time.time() - t0 < timeout_s:
        try:
            r = call("GET", f"/actor-runs/{run_id}", params={"waitForFinish": 60})["data"]
        except RuntimeError as e:
            if re.search(r"-> 4(?!08|29)\d\d:", str(e)):
                raise
            time.sleep(10); continue
        if r["status"] in DONE:
            return r
    with contextlib.suppress(RuntimeError):  # hung: stop it so it cannot keep billing
        call("POST", f"/actor-runs/{run_id}/abort", safe=True)
        r = call("GET", f"/actor-runs/{run_id}", params={"waitForFinish": 30})["data"]
    if r is None:
        raise RuntimeError(f"run {run_id} unreachable")
    return r


def items(dataset_id):
    out, offset = [], 0
    while True:
        page = call("GET", f"/datasets/{dataset_id}/items", tries=6,
                    params={"format": "json", "clean": "true", "offset": offset, "limit": 1000})
        out += page
        if len(page) < 1000:
            return out
        offset += 1000


def run(actor, run_input, max_items, cap_usd):
    """Start a run, wait for it and read its results. -> (run record, items)."""
    started = start(actor, run_input, max_items, cap_usd)
    try:
        r = wait(started["id"])
        got = items(r["defaultDatasetId"])
        with contextlib.suppress(RuntimeError):  # charges land a moment after the run ends: re-read for the real cost
            r = call("GET", f"/actor-runs/{r['id']}")["data"]
    except BaseException as e:
        orphan(started, actor, e)
        raise
    return r, got


def orphan(r, actor, e):
    why = _scrub(type(e).__name__ + (f": {e}" if str(e) else ""))
    with contextlib.suppress(Exception):
        common.append_jsonl(common.path("orphan_runs.jsonl"), [{"at": common.now().isoformat(timespec="seconds"),
                            "run": r.get("id"), "dataset": r.get("defaultDatasetId"), "actor": actor, "reason": why}])
    print(f"Apify run {r.get('id')} ({actor}) left unfinished or unread: {why}; see data/orphan_runs.jsonl", file=sys.stderr)
    with contextlib.suppress(Exception):
        call("POST", f"/actor-runs/{r['id']}/abort", safe=True, tries=1)


def cost(r):
    return float((r or {}).get("usageTotalUsd") or 0)
