"""The Home page "Gym" card: four numbers a GM reads each morning.

ROADMAP Block 23 (interviewed with Luke 2026-10-16). Chris's ask (10/14,
"CRM - insights"): "a live member count, and current month new member adds,
cancellations, and revenue". Luke's rulings, in short:

  * just those four numbers, no comparisons, no splits, not clickable;
  * window = the last 30 days (rolling), not the calendar month;
  * count PEOPLE, not plans: a plan switch (cancel + another plan within about
    a month) is neither a cancel nor a new member; a cancel = the person has no
    active plan left (same rule as the SHIFT retention study, Block 22);
  * members today = active and paying; frozen members are left out of the
    number and shown on a small line under it;
  * revenue = net sales: everything sold, before sales tax, minus refunds.

The SAME file sits in both gyms' trees (CLAUDE.md rule 4) and holds both data
paths, because the two gyms' systems record these facts differently:

  Beta (SHIFT)  Memberships.csv carries created_date / cancelled_date /
                frozen_date per plan row, so all three member numbers come
                from it directly. Transactions.csv carries total_less_tax.
  RGP (ABC)     RGP keeps no cancel dates and no record of a status change
                (member_counts.py header). So ABC writes its own history:
                member_status_history.csv, one row per person per CHANGE of
                status, appended by the 3 AM rgp-pull cron (record_history
                below). Cancels can only be counted from the day that history
                starts; the card says "since <date>" until it covers 30 days.
                New members come from the membership purchases in
                transactions.csv (first purchase, or a comeback after
                the last one ran out; rgp_new_members).
                Revenue is read from transactions.csv's items text, whose
                "($price)" is RGP's line total (20 day passes = $320 on one
                line; checked on the 6/17 invoice pull, 2026-10-16).

Block 24 (Chris's 10/16 call, interviewed with Luke the same day) adds two
periods and a comparison for every number (this REVERSES Block 23's "no
comparisons"):

  * "month" = this calendar month so far, compared with the SAME days of
    last month (Oct 1-16 vs Sep 1-16; a longer month is capped at the
    shorter month's last day). This is the page's default.
  * "r30"   = the last 30 days, compared with the 30 days before.
  * Members (a head count, not a flow) compares today with the same date
    last month ("month") or 30 days ago ("r30").

Both periods end on the day the data runs through (the latest sale in the
transactions file, never after today), not on today: the files are pulled
at 3 AM, so today has no sales or new plans yet, and ending on today would
compare 15 days of data with 16 and tilt every arrow down.

Nothing here sends, writes to Drive, or raises into a page render: build_*
returns {"error": ...} on any surprise and the card shows that instead.
"""
from __future__ import annotations

import csv
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

WINDOW_DAYS = 30
SWITCH_GAP = 31          # days: a new plan this soon after a cancel = a plan switch

# RGP revenue categories that are not sales. Gift card sales are money held for
# a later visit (the visit itself is counted when the card is spent), and the
# other three are bookkeeping lines. Matched case-insensitively.
RGP_NOT_SALES = {"gift cards", "accounting", "closeout adjustment", "petty cash"}

HISTORY_FIELDS = ["date", "climber_key", "status"]
GONE = "GONE"            # history status for a person who left the member file


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _d(v) -> date | None:
    """'2026-08-24 17:58:24 -0400 EDT' / '2026-05-17' -> date; junk -> None.
    RGP's null date (0000-00-00) is junk too."""
    s = str(v or "").strip()[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _f(v) -> float:
    try:
        return float(str(v).strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _rows(src) -> list[dict]:
    """A CSV path -> its rows; an already-read list passes through (build_*
    reads each file once and counts four windows from it)."""
    if isinstance(src, list):
        return src
    p = Path(src)
    if not p.exists():
        return []
    with open(p, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def window(today: date) -> tuple[date, date]:
    """The last 30 days, today included."""
    return today - timedelta(days=WINDOW_DAYS - 1), today


def month_back(d: date) -> date:
    """The same date last month; Mar 31 -> Feb 28 (capped at the month's end)."""
    first_prev = (d.replace(day=1) - timedelta(days=1)).replace(day=1)
    last_prev = d.replace(day=1) - timedelta(days=1)
    return min(first_prev + timedelta(days=d.day - 1), last_prev)


PERIODS = ("month", "r30")


def periods(ref: date) -> dict:
    """{"month"|"r30": (start, end, prev_start, prev_end)} ending on `ref`.
    month: the 1st..ref vs the 1st of last month..the same day last month.
    r30:   the last 30 days vs the 30 days before them."""
    s30, e30 = window(ref)
    return {
        "month": (ref.replace(day=1), ref, month_back(ref).replace(day=1), month_back(ref)),
        "r30": (s30, e30, s30 - timedelta(days=WINDOW_DAYS), s30 - timedelta(days=1)),
    }


def compare_dates(today: date) -> dict:
    """The day a head count is compared with: same date last month / 30 days ago."""
    return {"month": month_back(today), "r30": today - timedelta(days=WINDOW_DAYS)}


def data_through(transactions_rows, today: date) -> date:
    """The latest sale date in the file, never after today (today if none)."""
    latest = None
    for r in transactions_rows:
        d = _d(r.get("time"))
        if d is not None and d <= today and (latest is None or d > latest):
            latest = d
    return latest or today


def _base(today: date, ref: date | None = None) -> dict:
    ref = ref or today
    start, end = window(ref)
    return {"as_of": today.isoformat(), "data_through": ref.isoformat(), "window_days": WINDOW_DAYS,
            "window_start": start.isoformat(), "window_end": end.isoformat()}


def _period_block(start, end, prev_start, prev_end) -> dict:
    return {"start": start.isoformat(), "end": end.isoformat(),
            "prev_start": prev_start.isoformat(), "prev_end": prev_end.isoformat()}


# Each item in a Beta or RGP-shaped items string: "<qty> <desc> ($<price>) $<price>".
# Beta ends every item with ";" and joins them with NO space ("...$6.00;1 Gift..."),
# rgp_ingest joins with "; " and prefixes "[<revenue category>] ". Split on ";"
# plus any spaces (fresh-eyes tester 2026-10-16: splitting on "; " read every
# multi-item Beta sale as one item). Checked: 0 unparsed parts at either gym.
_PART = re.compile(r"^\s*(-?\d+)\s+(?:\[([^\]]*)\]\s*)?(.*?)\s*"
                   r"\(\$(-?\d+(?:\.\d+)?)\)\s+\$(-?\d+(?:\.\d+)?)\s*$")
_SPLIT = re.compile(r";\s*")


def _items(items: str):
    """Yield (category, description, line_total) per item; (None, part, None)
    for a part that does not parse, so the caller can count it."""
    for part in _SPLIT.split(str(items or "")):
        part = part.strip()
        if not part:
            continue
        m = _PART.match(part)
        if not m:
            yield None, part, None
            continue
        yield (m.group(2) or "").strip(), m.group(3).strip(), float(m.group(5))


# --------------------------------------------------------------------------
# revenue
# --------------------------------------------------------------------------

def beta_net_sales(transactions_csv, start: date, end: date) -> dict:
    """Net sales from Beta's Transactions.csv for start..end, counted the way
    Beta's own sales report counts them (matched to the cent-ish on Jadee's
    September 2026 report, 2026-10-19: sales $23,187.82, refunds $418, net
    $22,769.82; ours $22,769.35, the 47 cents being a $0.50 partial refund's
    tax share):

      * a sale counts on the day it was PAID (time_finalized; the sale's own
        time when that is blank), at total_less_tax (Beta's "before tax"),
        for states SUCCEEDED and REFUNDED. An autobill that failed and was
        paid weeks later counts in the month it was paid;
      * a refund comes off on the day it was made (time_refunded; the sale
        day when blank), at its before-tax share (Beta's refund figure
        includes tax). So a July sale refunded in September lowers September.
    Out: OPEN (not paid yet) and CANCELLED, and Gift Card items (money held
    for a later visit). A gift card never takes a sale below zero: Beta lists
    the card at its face value even when it was free or discounted (promos),
    so the most it can remove is what the sale itself took in.
    Before 2026-10-19 a refund was counted against the sale day and REFUNDED
    sales were left out; with the 2-day pull window, later payments and
    refunds never reached the file at all (beta_pull --tx-days)."""
    total = 0.0
    n = 0
    gift = 0.0
    refunds = 0.0
    latest = None
    for r in _rows(transactions_csv):
        d = _d(r.get("time"))
        if d is None:
            continue
        if latest is None or d > latest:
            latest = d
        if str(r.get("state", "")).strip().upper() not in ("SUCCEEDED", "REFUNDED"):
            continue
        less_tax, gross = _f(r.get("total_less_tax")), _f(r.get("total_amount"))
        paid = _d(r.get("time_finalized")) or d
        if start <= paid <= end:
            amt = less_tax
            for _cat, desc, line in _items(r.get("items")):
                if line is not None and line > 0 and "gift card" in desc.lower():
                    take = min(line, max(amt, 0.0))
                    amt -= take
                    gift += take
            total += amt
            n += 1
        refund = _f(r.get("total_refunds"))
        if refund:
            if gross > 0:
                refund *= less_tax / gross           # the refund's before-tax share
            rd = _d(r.get("time_refunded")) or paid
            if start <= rd <= end:
                total -= refund
                refunds += refund
    return {"amount": round(total, 2), "sales": n, "refunds": round(refunds, 2),
            "gift_cards_left_out": round(gift, 2),
            "through": latest.isoformat() if latest else ""}


def rgp_net_sales(transactions_csv, start: date, end: date) -> dict:
    """Net sales from ABC's transactions.csv (rebuilt from RGP invoice lines by
    rgp_ingest) for start..end. Every line of a non-voided invoice counts, at
    its line total, refunds included (RGP writes a refund as a negative line).
    Sales tax never reaches this file (rgp_pull drops the tax lines). Lines in
    RGP_NOT_SALES are left out."""
    total = 0.0
    n = 0
    unparsed = 0
    latest = None
    for r in _rows(transactions_csv):
        d = _d(r.get("time"))
        if d is None:
            continue
        if latest is None or d > latest:
            latest = d
        if not (start <= d <= end) or str(r.get("state", "")).strip().upper() != "SUCCEEDED":
            continue
        counted = False
        for cat, _desc, line in _items(r.get("items")):
            if line is None:
                unparsed += 1
                continue
            if (cat or "").lower() in RGP_NOT_SALES:
                continue
            total += line
            counted = True
        n += counted
    return {"amount": round(total, 2), "sales": n, "unparsed_items": unparsed,
            "through": latest.isoformat() if latest else ""}


# --------------------------------------------------------------------------
# members: Beta (SHIFT)
# --------------------------------------------------------------------------

def _beta_episodes(memberships_csv, today: date) -> tuple[list[dict], dict]:
    """Chain each person's plan rows into episodes (continuous time as a
    member), the retention study's rule: a row joins the open episode, or one
    that ended within SWITCH_GAP days. Freeze-fee rows and staff plans are not
    plans a member pays for, so they are skipped (counted in `skipped`).
    A cancelled_date after today means the plan is still running."""
    skipped = defaultdict(int)
    by_person: dict[str, list[dict]] = defaultdict(list)
    for r in _rows(memberships_csv):
        cid = str(r.get("climber_key") or "").strip()
        start = _d(r.get("created_date"))
        if not cid or start is None or start > today:
            skipped["no person or start date"] += 1
            continue
        prices = str(r.get("prices") or "")
        parts = [x.strip().lower() for x in prices.split("|") if x.strip()]
        if parts and all("freeze fee" in x for x in parts):
            skipped["freeze-fee rows"] += 1
            continue
        blob = " ".join(str(r.get(k) or "") for k in ("prices", "perpetual_discount", "metadata")).lower()
        if "staff" in blob:
            skipped["staff plans"] += 1
            continue
        end = _d(r.get("cancelled_date"))
        if end is not None and end > today:
            end = None
        fz, ufz = _d(r.get("frozen_date")), _d(r.get("unfreeze_date"))
        frozen = fz is not None and fz <= today and (ufz is None or ufz > today)
        by_person[cid].append({"start": start, "end": end, "frozen": frozen})

    episodes = []
    for cid, rows in by_person.items():
        rows.sort(key=lambda x: x["start"])
        cur = None
        for r in rows:
            if cur is not None and (cur["end"] is None or
                                    r["start"] <= cur["end"] + timedelta(days=SWITCH_GAP)):
                if r["end"] is None:
                    cur["end"] = None
                elif cur["end"] is not None and r["end"] > cur["end"]:
                    cur["end"] = r["end"]
                if r["end"] is None:
                    cur["open_rows"].append(r)
                continue
            cur = {"cid": cid, "start": r["start"], "end": r["end"],
                   "open_rows": [r] if r["end"] is None else []}
            episodes.append(cur)
    return episodes, dict(skipped)


def _beta_members_on(mem_rows, day: date) -> dict:
    """Active (paying) and frozen people on `day`, from the plan rows."""
    eps, _skipped = _beta_episodes(mem_rows, day)
    open_eps = [e for e in eps if e["end"] is None]
    frozen = [e for e in open_eps if e["open_rows"] and all(r["frozen"] for r in e["open_rows"])]
    return {"active": len(open_eps) - len(frozen), "frozen": len(frozen)}


def _beta_flows(eps, start: date, end: date) -> tuple[int, int]:
    """(new members, cancellations) in start..end. A cancel = an episode that
    ended in the window, for a person who is not a member on the window's
    last day (for the current window that is Block 23's rule: holds no plan
    today)."""
    member_at_end = {e["cid"] for e in eps
                     if e["start"] <= end and (e["end"] is None or e["end"] > end)}
    new = {e["cid"] for e in eps if start <= e["start"] <= end}
    cancels = {e["cid"] for e in eps
               if e["end"] is not None and start <= e["end"] <= end
               and e["cid"] not in member_at_end}
    return len(new), len(cancels)


def build_beta(memberships_csv, transactions_csv, today: date) -> dict:
    """The card's block for a Beta gym (SHIFT). Never raises."""
    out = _base(today)
    try:
        mem_rows, tx_rows = _rows(memberships_csv), _rows(transactions_csv)
        ref = data_through(tx_rows, today)
        out = _base(today, ref)
        eps, skipped = _beta_episodes(mem_rows, today)
        members = _beta_members_on(mem_rows, today)
        members["prev"] = {}
        for key, day in compare_dates(today).items():
            members["prev"][key] = {"date": day.isoformat(), "active": _beta_members_on(mem_rows, day)["active"]}
        pers = {}
        for key, (s, e, ps, pe) in periods(ref).items():
            n_new, n_can = _beta_flows(eps, s, e)
            p_new, p_can = _beta_flows(eps, ps, pe)
            blk = _period_block(s, e, ps, pe)
            blk.update({
                "new_members": n_new, "prev_new_members": p_new,
                "cancels": {"n": n_can, "since": None}, "prev_cancels": {"n": p_can, "since": None},
                "net_sales": beta_net_sales(tx_rows, s, e),
                "prev_net_sales": beta_net_sales(tx_rows, ps, pe),
            })
            pers[key] = blk
        r30 = pers["r30"]
        out.update({
            "source": "beta",
            "members": members,
            "periods": pers,
            # Block 23's fields, kept = the last-30-days period
            "new_members": r30["new_members"],
            "cancels": r30["cancels"],
            "net_sales": r30["net_sales"],
            "skipped": skipped,
        })
    except Exception as exc:                          # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


# --------------------------------------------------------------------------
# members: RGP (ABC)
# --------------------------------------------------------------------------

def _status(v) -> str:
    s = str(v or "").strip().upper()
    return s or "(BLANK)"


def _active_like(s: str) -> bool:
    """Still a member: paying (OK) or paused (FROZEN)."""
    return s in ("OK", "FROZEN")


def history_path(member_status_csv) -> Path:
    return Path(member_status_csv).with_name("member_status_history.csv")


def load_history(path) -> list[dict]:
    return [{k: str(r.get(k) or "").strip() for k in HISTORY_FIELDS} for r in _rows(path)]


def last_status(history: list[dict]) -> dict:
    """climber_key -> latest recorded status (file order breaks same-day ties)."""
    out: dict = {}
    for r in sorted(enumerate(history), key=lambda t: (t[1]["date"], t[0])):
        out[r[1]["climber_key"]] = r[1]["status"]
    return out


def record_history(history_csv, member_status_csv, today: date) -> dict:
    """Append today's status CHANGES to the history file (ABC's 3 AM cron).

    First run (no file) writes everyone's status as the starting point. After
    that only a person whose status differs from their last recorded one gets
    a row; a person who dropped out of the member file gets GONE. A missed day
    costs nothing: the change lands on the next run's date. Refuses an empty
    member file (a bad pull must not mark the whole gym GONE). Returns counts
    for the report; raises on a real error so the caller can print FAILED."""
    cur = {str(r.get("climber_key") or "").strip(): _status(r.get("status"))
           for r in _rows(member_status_csv)}
    cur.pop("", None)
    if not cur:
        raise RuntimeError(f"member file is empty or missing ({member_status_csv}); history not touched")
    path = Path(history_csv)
    hist = load_history(path)
    last = last_status(hist)
    first_run = not hist
    stamp = today.isoformat()
    new_rows = []
    for k, s in sorted(cur.items()):
        if last.get(k) != s:
            new_rows.append({"date": stamp, "climber_key": k, "status": s})
    for k, s in sorted(last.items()):
        if k not in cur and s != GONE:
            new_rows.append({"date": stamp, "climber_key": k, "status": GONE})
    left = sum(1 for r in new_rows if not first_run and not _active_like(r["status"])
               and _active_like(last.get(r["climber_key"], "")))
    joined = sum(1 for r in new_rows if not first_run and r["status"] == "OK"
                 and not _active_like(last.get(r["climber_key"], "")))
    path.parent.mkdir(parents=True, exist_ok=True)
    # an empty file counts as new: the Drive file starts as a 0-byte CSV
    new_file = not path.exists() or path.stat().st_size == 0
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        if new_file:
            w.writeheader()
        w.writerows(new_rows)
    return {"first_run": first_run, "changes": len(new_rows), "left": left,
            "joined_or_back": joined, "rows": len(hist) + len(new_rows),
            "people": len(cur), "since": (hist[0]["date"] if hist else stamp)}


def _rgp_left(history: list[dict], start: date, end: date) -> tuple[set, str]:
    """(people who stopped being a member in the window, first history date).
    Left = went from OK/FROZEN to anything else and is not back as OK/FROZEN
    within SWITCH_GAP days (or by today). The starting rows are not changes."""
    by_person: dict[str, list[tuple[date, int, str]]] = defaultdict(list)
    first = None
    for i, r in enumerate(history):
        d = _d(r["date"])
        if d is None:
            continue
        first = d if first is None or d < first else first
        by_person[r["climber_key"]].append((d, i, r["status"]))
    left = set()
    for cid, evs in by_person.items():
        evs.sort()
        for j in range(1, len(evs)):
            (_pd, _pi, ps), (d, _i, s) = evs[j - 1], evs[j]
            if _active_like(ps) and not _active_like(s) and start <= d <= end:
                nxt = next((e for e in evs[j + 1:] if _active_like(e[2])), None)
                if nxt is None or (nxt[0] - d).days > SWITCH_GAP:
                    left.add(cid)
    return left, (first.isoformat() if first else "")


def _rgp_cover(desc: str) -> tuple[int, bool]:
    """(days a membership purchase covers, whether it is a join/renewal).
    From RGP's item names (checked 2026-10-16): "Monthly Dues", "One Month
    Prepaid", "6 Month Prepaid", "Annual Prepaid", "1 Year Anniversary",
    "Student Summer Membership (May to August)", "3 Month Prepaid ...".
    A freeze payment keeps someone a member but is not a join; a cancellation
    fee or card surcharge is neither."""
    d = desc.lower()
    if "cancellation fee" in d or "surcharge" in d:
        return 0, False
    if "freeze" in d:
        return 31, False
    if "annual" in d or "year" in d:
        return 366, True
    if "6 month" in d or "6mo" in d:
        return 183, True
    if "3 month" in d:
        return 92, True
    if "summer" in d:
        return 123, True
    return 31, True


def rgp_join_events(transactions_csv) -> list[tuple[date, str]]:
    """(date, person) for every membership purchase that is either the
    person's first ever, or comes more than SWITCH_GAP days after their
    earlier membership ran out (a comeback). The run-out date uses each
    purchase's length, so a one-month or six-month prepaid member renewing a
    few days late is a renewal, not a new member (fresh-eyes tester
    2026-10-16). The person on the invoice (the payer). A purchase is judged
    only on the ones before it, so one list serves every window."""
    buys: dict[str, list[tuple[date, int, bool]]] = defaultdict(list)
    for r in _rows(transactions_csv):
        if str(r.get("state", "")).strip().upper() != "SUCCEEDED":
            continue
        d = _d(r.get("time"))
        cid = str(r.get("climber_id") or "").strip()
        if d is None or not cid:
            continue
        for cat, desc, line in _items(r.get("items")):
            if (cat or "").lower() == "membership" and line is not None and line > 0.011:
                cover, join = _rgp_cover(desc)
                if cover:
                    buys[cid].append((d, cover, join))
    events = []
    for cid, bl in buys.items():
        bl.sort()
        covered_until = None
        for d, cover, join in bl:
            if join and (covered_until is None or (d - covered_until).days > SWITCH_GAP):
                events.append((d, cid))
            run_out = d + timedelta(days=cover)
            covered_until = run_out if covered_until is None else max(covered_until, run_out)
    return events


def rgp_new_members(transactions_csv, start: date, end: date, events=None) -> set:
    """People with a join (rgp_join_events) in start..end."""
    evs = rgp_join_events(transactions_csv) if events is None else events
    return {cid for d, cid in evs if start <= d <= end}


def _rgp_members_on(day: date, hist: list[dict], count_rows: list[dict]) -> dict | None:
    """{"date", "active", "source"} for the member head count on `day`, or None.
    From the status history once it covers that day (exact: people whose
    latest status on or before it is OK), else from the weekly member-count
    log (member_counts.py, written since 2026-09) taken within the week
    before. member_status.csv itself is only ever today's list."""
    first = min((_d(r["date"]) for r in hist if _d(r["date"])), default=None)
    if first is not None and first <= day:
        last: dict = {}
        for _i, r in sorted(enumerate(hist), key=lambda t: (t[1]["date"], t[0])):
            if _d(r["date"]) and _d(r["date"]) <= day:
                last[r["climber_key"]] = r["status"]
        return {"date": day.isoformat(), "active": sum(1 for s in last.values() if s == "OK"),
                "source": "history"}
    best = None
    for r in count_rows or []:
        rd = _d(r.get("recorded_at"))
        if rd is None or rd > day or (day - rd).days > 7:
            continue
        try:
            n = int(str(r.get("active") or "").strip())
        except ValueError:
            continue
        if best is None or rd >= best[0]:
            best = (rd, n)
    if best is None:
        return None
    return {"date": best[0].isoformat(), "active": best[1], "source": "weekly count"}


def _rgp_cancels(hist: list[dict], start: date, end: date) -> dict:
    """{"n", "since"} for start..end. n is None with no history; "since" is
    the day the history began when that falls after the window's start
    (the window is only partly covered)."""
    left, first = _rgp_left(hist, start, end)
    since = first if first and _d(first) > start else None
    return {"n": len(left) if first else None, "since": since}


def build_rgp(member_status_csv, transactions_csv, today: date, history_csv=None,
              count_rows=None) -> dict:
    """The card's block for an RGP gym (ABC). Never raises.

    members: member_status.csv, OK vs FROZEN (the same count as the Members
    card's "Active members"); the earlier head count from the history or the
    weekly member-count log (count_rows = member_counts.load_rows). new:
    rgp_join_events (first membership purchase, or a comeback after the last
    one ran out). cancels: from the history only, so an earlier period the
    history does not fully cover has no number (and the page no arrow)."""
    out = _base(today)
    try:
        statuses = [_status(r.get("status")) for r in _rows(member_status_csv)]
        if not statuses:
            raise RuntimeError("member_status.csv is missing or empty")
        tx_rows = _rows(transactions_csv)
        ref = data_through(tx_rows, today)
        out = _base(today, ref)
        events = rgp_join_events(tx_rows)
        hist = load_history(history_csv or history_path(member_status_csv))
        members = {"active": statuses.count("OK"), "frozen": statuses.count("FROZEN"), "prev": {}}
        for key, day in compare_dates(today).items():
            members["prev"][key] = _rgp_members_on(day, hist, count_rows)
        pers = {}
        for key, (s, e, ps, pe) in periods(ref).items():
            blk = _period_block(s, e, ps, pe)
            prev_c = _rgp_cancels(hist, ps, pe)
            if prev_c["since"]:                       # the history does not cover it all
                prev_c = {"n": None, "since": prev_c["since"]}
            blk.update({
                "new_members": len(rgp_new_members(None, s, e, events)),
                "prev_new_members": len(rgp_new_members(None, ps, pe, events)),
                "cancels": _rgp_cancels(hist, s, e), "prev_cancels": prev_c,
                "net_sales": rgp_net_sales(tx_rows, s, e),
                "prev_net_sales": rgp_net_sales(tx_rows, ps, pe),
            })
            pers[key] = blk
        r30 = pers["r30"]
        out.update({
            "source": "rgp",
            "members": members,
            "periods": pers,
            # Block 23's fields, kept = the last-30-days period
            "new_members": r30["new_members"],
            "cancels": dict(r30["cancels"],
                            tracking_starts="" if hist else "the next morning data refresh"),
            "net_sales": r30["net_sales"],
        })
    except Exception as exc:                          # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def eastern_today() -> date:
    """Both gyms are on Eastern time; the web host runs on UTC."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:                                 # noqa: BLE001 - no tz database
        return (datetime.utcnow() - timedelta(hours=4)).date()
