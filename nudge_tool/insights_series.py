"""Insights chart builder: the month-by-month numbers that are not about
first-timers (ROADMAP Block 28, step 3; Luke's 2026-11-10 picks from Chris's
PowerBI notes): member movement, pass + membership sales, and how long
today's members have been members.

SHARED MODULE: byte-identical in the ABC and SHIFT trees (project rule 4).
Every number is counted by gym_metrics' own functions (the Home page "Gym"
card), one calendar month at a time, so a month here and the same days on
Home can never disagree about a rule:

  new members / cancellations   _beta_flows (SHIFT), rgp_join_events +
                                _rgp_cancels (ABC)
  member count at month end     _beta_members_on (SHIFT), _rgp_members_on (ABC)
  sales                         the net sales rule (before sales tax, minus
                                refunds, gift cards out), split by what was
                                sold. The three parts (passes, memberships,
                                everything else) add up to gym_metrics'
                                net sales for the month, to the cent.

What each gym's system cannot say ships as None with a plain note, never a
guess (ABC: no cancellations before its status history began, no member
count before the weekly log began, no frozen count for past months).

Nothing here identifies a person: counts and dollars per month, and plan
names read from the gym's price list (ABC's membership lines carry the
member's name, "Monthly Dues for <name>", so ABC plans are reduced to the
plan length by keyword and the line text is never shipped).

Pure read + shape. No network. Never raises into a page: build() returns
{"enabled": False, "error": ...} on any surprise.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from . import gym_metrics as gm
from . import ingest
from .config import ClientConfig

VERSION = 1
MAX_MONTHS = 60

# Product categories (client.json reporting.product_rules) that are a pass:
# an entry someone buys without becoming a member. Trials are passes here
# (Claude 2026-11-10, flagged to Luke: a trial is closer to a week pass than
# to dues). Any other category is "everything else" (retail, rentals,
# classes, events) and is not shown as its own number in this build.
PASS_CATEGORIES = ("DAY_PASS", "PUNCH", "TRIAL_2WK", "TRIAL_30DAY")
MEMBERSHIP_CATEGORY = "MEMBERSHIP"
MEMBERSHIPS = "Memberships"
OTHER = "Everything else"

LENGTH_BANDS = (("Under 3 months", 0, 3), ("3 to 6 months", 3, 6), ("6 to 12 months", 6, 12),
                ("1 to 2 years", 12, 24), ("2 years or more", 24, 10000))


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _next_month(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1)


def months_between(first: date, last: date) -> list[date]:
    """The first day of every month from `first`'s month to `last`'s, oldest
    first, at most MAX_MONTHS (the newest ones)."""
    out, m = [], _month_start(first)
    while m <= last:
        out.append(m)
        m = _next_month(m)
    return out[-MAX_MONTHS:]


def _key(m: date) -> str:
    return m.isoformat()[:7]


# --------------------------------------------------------------------------
# sales, split by what was sold
# --------------------------------------------------------------------------

def _line_kind(cat_tag: str, desc: str, rules, default, pass_words, excl_words,
               plan_names: set, labels: dict) -> tuple[str, str]:
    """(kind, product label) for one sold line. kind = "pass" | "membership"
    | "other". cat_tag = RGP's "[Category]" (ABC), "" at a Beta gym."""
    name = (("[" + cat_tag + "] ") if cat_tag else "") + desc
    low = name.lower()
    cat = ingest.categorize_product(name, rules, default)
    if cat == MEMBERSHIP_CATEGORY or desc.strip().lower() in plan_names:
        return "membership", MEMBERSHIPS
    if cat in PASS_CATEGORIES:
        # RGP files rentals, leagues and gift certificates under Day Use too:
        # with pass words configured, a Day Use line must name a pass
        # (the same test as Block 16's group_pass_count; a punch card counts)
        if cat == "DAY_PASS" and pass_words and cat_tag:
            if not (any(w in low for w in pass_words) or "punch" in low) or any(w in low for w in excl_words):
                return "other", OTHER
        return "pass", labels.get(cat, cat)
    return "other", OTHER


def _empty_sales() -> dict:
    return {"pass": 0.0, "membership": 0.0, "other": 0.0, "pass_qty": 0, "products": defaultdict(float),
            "product_qty": defaultdict(int)}


def sales_by_month(client: ClientConfig, tx_rows: list, source: str, plan_names: set) -> dict:
    """{YYYY-MM: {"pass", "membership", "other", "pass_qty", "products"}}.
    The same rule as gym_metrics.beta_net_sales / rgp_net_sales, line by
    line. Beta charges a discount on the whole sale, so each line gets its
    share of what the sale took in (and of any refund, on the refund day)."""
    rep = client.reporting or {}
    rules = rep.get("product_rules", [])
    default = rep.get("product_default_category", "RETAIL")
    labels = rep.get("entry_product_labels", {})
    grp = rep.get("group_visit") or {}
    pass_words = [w.lower() for w in grp.get("pass_words", []) if w]
    excl_words = [w.lower() for w in grp.get("exclude_words", []) if w]
    out: dict[str, dict] = defaultdict(_empty_sales)

    def kind_of(cat_tag, desc):
        return _line_kind(cat_tag or "", desc, rules, default, pass_words, excl_words, plan_names, labels)

    def add(day: date, kind: str, label: str, amount: float, qty: int = 0) -> None:
        b = out[_key(day)]
        b[kind] += amount
        if kind == "pass":
            b["pass_qty"] += qty
            b["products"][label] += amount
            b["product_qty"][label] += qty

    for r in tx_rows:
        d = gm._d(r.get("time"))
        if d is None:
            continue
        state = str(r.get("state", "")).strip().upper()
        if source == "rgp":
            if state != "SUCCEEDED":
                continue
            for (cat, desc, line), q in zip(gm._items(r.get("items")), _qtys(r.get("items"))):
                if line is None or (cat or "").lower() in gm.RGP_NOT_SALES:
                    continue
                kind, label = kind_of(cat, desc)
                # RGP writes a refund as a negative line: it takes the passes back out
                add(d, kind, label, line, abs(q) if line > 0 else -abs(q) if line < 0 else 0)
            continue
        if state not in ("SUCCEEDED", "REFUNDED"):
            continue
        less_tax, gross = gm._f(r.get("total_less_tax")), gm._f(r.get("total_amount"))
        paid = gm._d(r.get("time_finalized")) or d
        lines = [(desc, line, q) for (_c, desc, line), q in zip(gm._items(r.get("items")), _qtys(r.get("items")))
                 if line is not None]
        # gift cards come out first, exactly as beta_net_sales does it
        amt = less_tax
        for desc, line, _q in lines:
            if line > 0 and "gift card" in desc.lower():
                amt -= min(line, max(amt, 0.0))
        sold = [(desc, line, q) for desc, line, q in lines if line > 0 and "gift card" not in desc.lower()]
        base = sum(line for _d2, line, _q in sold)
        shares = [(kind_of("", desc), line / base, q) for desc, line, q in sold] if base > 0 else []
        if not shares:
            shares = [(("other", OTHER), 1.0, 0)]
        for (kind, label), share, q in shares:
            add(paid, kind, label, amt * share, q)
        refund = gm._f(r.get("total_refunds"))
        if refund:
            if gross > 0:
                refund *= less_tax / gross
            rd = gm._d(r.get("time_refunded")) or paid
            for (kind, label), share, _q in shares:
                add(rd, kind, label, -refund * share)
    return out


def _qtys(items: str) -> list[int]:
    """The quantity in front of each item, in gym_metrics._items order."""
    out = []
    for part in gm._SPLIT.split(str(items or "")):
        part = part.strip()
        if not part:
            continue
        m = gm._PART.match(part)
        out.append(int(m.group(1)) if m else 0)
    return out


# --------------------------------------------------------------------------
# members
# --------------------------------------------------------------------------

def _bands(starts: list[date], today: date) -> list[dict]:
    months = [(today - s).days / gm.MONTH_DAYS for s in starts if s is not None and s <= today]
    return [{"label": label, "n": sum(1 for m in months if lo <= m < hi)} for label, lo, hi in LENGTH_BANDS]


def _rgp_plan(desc: str) -> str:
    """ABC's plan, by length only: the line text names the member."""
    d = desc.lower()
    if "annual" in d or "year" in d:
        return "Annual prepaid"
    if "6 month" in d or "6mo" in d:
        return "6 month prepaid"
    if "3 month" in d:
        return "3 month prepaid"
    if "summer" in d:
        return "Summer membership"
    if "prepaid" in d:
        return "1 month prepaid"
    return "Monthly dues"


def _by_plan(pairs: list[tuple[str, date]], today: date) -> list[dict]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for plan, start in pairs:
        grouped[plan].append((today - start).days / gm.MONTH_DAYS)
    rows = [{"label": p, "n": len(v), "avg_months": round(sum(v) / len(v), 1)} for p, v in grouped.items()]
    return sorted(rows, key=lambda r: -r["n"])


def _beta_members(client: ClientConfig, today: date, months: list[date]) -> tuple[dict, dict, set]:
    mem_rows = gm._rows(client.memberships_csv)
    eps, _skipped = gm._beta_episodes(mem_rows, today)
    series = {k: [] for k in ("new", "cancels", "active", "frozen")}
    for m in months:
        end = min(_next_month(m) - timedelta(days=1), today)
        n_new, n_can = gm._beta_flows(eps, m, end)
        on = gm._beta_members_on(mem_rows, end)
        series["new"].append(n_new)
        series["cancels"].append(n_can)
        series["active"].append(on["active"])
        series["frozen"].append(on["frozen"])
    paying, _frozen = gm._beta_active_eps(eps)
    # each paying member's plan today: the price-list name of a running plan row
    plan_now: dict[str, str] = {}
    plan_names: set = set()
    for r in mem_rows:
        parts = [p.strip() for p in str(r.get("prices") or "").split("|") if p.strip()]
        names = [p.rsplit("(", 1)[0].strip() for p in parts]
        names = [n for n in names if n and "freeze fee" not in n.lower()]
        plan_names.update(n.lower() for n in names)
        cid = str(r.get("climber_key") or "").strip()
        end = gm._d(r.get("cancelled_date"))
        if cid and names and (end is None or end > today):
            plan_now.setdefault(cid, names[0])
    starts = [e["start"] for e in paying]
    length = {"bands": _bands(starts, today), "n": len(starts), "of": len(paying),
              "by_plan": _by_plan([(plan_now.get(e["cid"], "Plan not named"), e["start"]) for e in paying], today)}
    notes = {}
    first = min((gm._d(r.get("created_date")) for r in mem_rows if gm._d(r.get("created_date"))), default=None)
    if first:
        notes["members"] = "Membership records start " + first.isoformat() + "."
    return series, {"length": length, "notes": notes}, plan_names


def _rgp_members(client: ClientConfig, today: date, months: list[date], tx_rows: list) -> tuple[dict, dict]:
    from . import member_counts
    ms_rows = gm._rows(client.member_status_csv)
    ok_keys = {str(r.get("climber_key") or "").strip() for r in ms_rows if gm._status(r.get("status")) == "OK"}
    ok_keys.discard("")
    frozen_now = sum(1 for r in ms_rows if gm._status(r.get("status")) == "FROZEN")
    buys = gm._rgp_buys(tx_rows)
    repeat = {cid for cid, bl in buys.items() if len(bl) > 1}
    visits = gm.rgp_visits(client.sessions_csv, ok_keys | repeat) or None
    events = gm.rgp_join_events(tx_rows, visits)
    hist = gm.load_history(gm.history_path(client.member_status_csv))
    try:
        count_rows = member_counts.load_rows(client)
    except Exception:                                 # noqa: BLE001 - no log, no earlier counts
        count_rows = []
    first_hist = min((gm._d(r["date"]) for r in hist if gm._d(r["date"])), default=None)
    series = {k: [] for k in ("new", "cancels", "active", "frozen")}
    for m in months:
        last = _next_month(m) - timedelta(days=1)
        end = min(last, today)
        series["new"].append(len(gm.rgp_new_members(None, m, end, events)))
        # a month the status history does not fully cover has no cancel count
        covered = first_hist is not None and first_hist <= m
        series["cancels"].append(len(gm._rgp_left(hist, m, end)[0]) if covered else None)
        if end == today:
            series["active"].append(len(ok_keys))
            series["frozen"].append(frozen_now)
        else:
            on = gm._rgp_members_on(end, hist, count_rows)
            series["active"].append(on["active"] if on else None)
            series["frozen"].append(None)
    starts = gm.rgp_stretch_starts(tx_rows, today, visits)
    dated = [k for k in ok_keys if k in starts]
    # the plan behind each dated member = their latest join / renewal line
    latest: dict[str, tuple[date, str]] = {}
    for r in tx_rows:
        if str(r.get("state", "")).strip().upper() != "SUCCEEDED":
            continue
        d, cid = gm._d(r.get("time")), str(r.get("climber_id") or "").strip()
        if d is None or cid not in starts or d > today:
            continue
        for cat, desc, line in gm._items(r.get("items")):
            if (cat or "").lower() == "membership" and line is not None and line > 0.011 and gm._rgp_cover(desc)[1]:
                if cid not in latest or d >= latest[cid][0]:
                    latest[cid] = (d, _rgp_plan(desc))
    length = {"bands": _bands([starts[k] for k in dated], today), "n": len(dated), "of": len(ok_keys),
              "by_plan": _by_plan([(latest[k][1] if k in latest else "Plan not named", starts[k]) for k in dated], today)}
    notes = {
        "cancels": ("Cancellations are counted from " + first_hist.isoformat() + ", the day this gym's member "
                    "status history began. Earlier months have no number.") if first_hist
                   else "This gym has no member status history yet, so cancellations have no number.",
        "active": "Member counts for past months come from the weekly member count log (since September 2026) "
                  "and the status history. Earlier months have no number.",
        "frozen": "Frozen members are known for today only.",
        "length": "Only members whose own membership purchases can be dated are counted "
                  f"({len(dated)} of {len(ok_keys)}); family members on someone else's plan are left out.",
    }
    return series, {"length": length, "notes": notes}


# --------------------------------------------------------------------------
# the payload block
# --------------------------------------------------------------------------

def build(client: ClientConfig, today: date) -> dict:
    """payload["insights"]["monthly"]: months + one list per number."""
    try:
        rgp = client.member_status_csv.exists()
        source = "rgp" if rgp else "beta"
        tx_rows = gm._rows(client.transactions_csv)
        ref = gm.data_through(tx_rows, today)
        first = min((gm._d(r.get("time")) for r in tx_rows if gm._d(r.get("time"))), default=None)
        if first is None:
            return {"enabled": False, "v": VERSION, "error": "no sales rows"}
        opening = gm._d((client.reporting or {}).get("post_opening_date"))
        months = months_between(max(first, opening) if opening else first, ref)
        if rgp:
            mem, extra = _rgp_members(client, today, months, tx_rows)
            plan_names: set = set()
        else:
            mem, extra, plan_names = _beta_members(client, today, months)
        sales = sales_by_month(client, tx_rows, source, plan_names)
        keys = [_key(m) for m in months]
        products = sorted({p for k in keys for p in sales[k]["products"]},
                          key=lambda p: -sum(sales[k]["products"].get(p, 0.0) for k in keys))
        net = [None if (n is None or c is None) else n - c for n, c in zip(mem["new"], mem["cancels"])]
        qty = [sales[k]["pass_qty"] for k in keys]
        pass_amt = [round(sales[k]["pass"], 2) for k in keys]
        return {
            "enabled": True,
            "v": VERSION,
            "source": source,
            "as_of": today.isoformat(),
            "data_through": ref.isoformat(),
            "months": keys,
            "members": {"new": mem["new"], "cancels": mem["cancels"], "net": net,
                        "active": mem["active"], "frozen": mem["frozen"]},
            "sales": {"pass": pass_amt,
                      "membership": [round(sales[k]["membership"], 2) for k in keys],
                      "other": [round(sales[k]["other"], 2) for k in keys],
                      "pass_qty": qty,
                      "products": [{"label": p,
                                    "amount": [round(sales[k]["products"].get(p, 0.0), 2) for k in keys],
                                    "qty": [sales[k]["product_qty"].get(p, 0) for k in keys]} for p in products]},
            "length": extra["length"],
            "notes": extra["notes"],
        }
    except Exception as exc:                          # noqa: BLE001
        print(f"  insights monthly numbers FAILED: {type(exc).__name__}: {exc}")
        return {"enabled": False, "v": VERSION, "error": f"{type(exc).__name__}: {exc}"}
