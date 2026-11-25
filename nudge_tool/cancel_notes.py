"""Cancel notes reader and the two cancel tabs on the All Gyms sheet (built
2026-11-25; shared, the same file in both trees. Plan: ABC SPEC.md, "Addition
2026-11-25: Cancel notes reader", later sub-sections win).

Why: learn why members leave and track it over time. Not a per-person tool.

Two tabs, both written by each gym's MORNING PULL (not the send run):
  Cancel Notes      SHIFT only. One row per cancelled plan from 2026-11-23,
                    Beta member id (no name, no email), the staff note(s)
                    written within 14 days either side of the cancel date, or
                    "no note". Rewritten whole each run, so a note edited or
                    deleted in Beta follows on the next run.
  Cancels by Month  Both gyms, one row per gym per month from 2026-11. A gym
                    rewrites its own rows and keeps the other gym's.

**Decision (2026-11-25):** the pull writes the tabs, not the 10 AM ET send run,
because the pull already holds the Beta login and the Google key; the send run
has no Beta login, so it would have needed two new secrets on a second service.
**Decision (2026-11-25):** the tab code lives here, not in allgyms_push.py, so
the send path is not touched at all.

Every number that also shows on the site comes from the site's own functions
(gym_metrics._beta_episodes / _beta_flows at SHIFT, _rgp_left at ABC), so
"Cancellations (people)" equals the Insights number by construction. The staff
plan and freeze-fee filter is _beta_episodes' own: a row is asked of it alone.

Values go to the sheet RAW, so staff text starting with "=" stays text.
"""
from __future__ import annotations

import csv
import io
import os
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from . import gym_metrics as gm

DETAIL_TAB = "Cancel Notes"
COUNTS_TAB = "Cancels by Month"
DETAIL_SINCE = date(2026, 11, 23)        # the price change; notes began 11/24
COUNTS_FIRST_MONTH = date(2026, 11, 1)   # November 2026 is counted whole
NOTE_WINDOW_DAYS = 14                    # a note this close to a cancel is its note
NO_NOTE = "no note"
NOTE_CELL_MAX = 5000                     # characters of note text kept per cell
GYM_TZ = "America/New_York"
_NUM_RETRIES = 5                         # project rule 1

# the same sheet allgyms_push.py writes (its module is not imported here: it
# pulls in the whole send stack). KEEP THE DEFAULT IDENTICAL TO allgyms_push.py.
ALLGYMS_SHEET_ID = os.getenv(
    "ALLGYMS_SHEET_ID", "1s4Cg7vZbriq1PjDGLPkc8PZUr2ou-XKK3Hr3I0QFaHo")

COUNTS_HEADER = ["Gym", "Month", "Cancelled plans", "Came back within a month",
                 "Cancellations (people)", "With a note", "Share with a note", "Updated"]
DETAIL_HEADER = ["Gym", "Beta member id", "Plan", "Plan start", "Cancel date",
                 "Came back within a month", "Counted as a cancellation on the site",
                 "Note", "Note author", "Note date", "Updated"]
COUNTS_NOTE = (
    "Cancelled plans = memberships that ended in the month. SHIFT: plan rows in Beta "
    "(staff plans and freeze fees left out). ABC: a membership that stopped, from the "
    "daily member status record (the gym system keeps one status per person, not plan "
    "rows). Two plans ending the same day are two cancelled plans. Came back within a month = the person kept or started another membership "
    "within 31 days, so the site does not count it. Cancellations (people) = the "
    "Cancellations number on the site's Insights page. Notes are SHIFT only and cover "
    "cancels from 2026-11-23 on; staff began writing cancel notes on 2026-11-24, so "
    "November 2026's share reads low. The current month is marked (so far).")
DETAIL_NOTE = (
    "SHIFT only. One row per cancelled plan from 2026-11-23 (staff plans and freeze "
    "fees left out). Note = what staff wrote on the member's Beta profile within 14 "
    "days either side of the cancel date; archived notes are left out. This tab is "
    "rewritten from Beta every morning. Two plans ending the same day are two rows, "
    "with one yes under Counted, so the yes rows add up to the site's number.")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _month_start(d: date) -> date:
    return d.replace(day=1)


def _next_month(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def _months(first: date, today: date) -> list[date]:
    out, m = [], _month_start(first)
    while m <= today:
        out.append(m)
        m = _next_month(m)
    return out


def _label(m: date, today: date) -> str:
    s = f"{m.year:04d}-{m.month:02d}"
    return s + " (so far)" if m == _month_start(today) else s


def _yn(v: bool) -> str:
    return "yes" if v else "no"


# date, clock time, optional fraction of a second, optional zone
_STAMP = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.\d+)?\s*([+-]\d{2}:?\d{2}|Z)?")


def _note_day(v) -> date | None:
    """Beta's '2026-11-24 15:28:08 +0000 UTC' -> the gym's (Eastern) date.
    A value with no clock time falls back to its own date; junk -> None."""
    s = str(v or "").strip()
    m = _STAMP.match(s)
    if not m or not m.group(3):
        return gm._d(s)
    zone = m.group(3).replace(":", "").replace("Z", "+0000")
    try:
        dt = datetime.strptime(f"{m.group(1)} {m.group(2)} {zone}", "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return gm._d(s)
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo(GYM_TZ)).date()
    except Exception:                                 # noqa: BLE001 - no tz database
        return (dt.astimezone(timezone.utc) - timedelta(hours=5)).date()


def _plan_name(prices) -> str:
    """'Unlimited (69.00 ...) | Freeze Fee (...)' -> 'Unlimited'."""
    names = []
    for part in str(prices or "").split("|"):
        name = part.strip().rsplit("(", 1)[0].strip()
        if name and "freeze fee" not in name.lower() and name not in names:
            names.append(name)
    return " + ".join(names)


# --------------------------------------------------------------------------
# notes (Beta's export)
# --------------------------------------------------------------------------

NOTE_COLUMNS = {"note", "issue_time", "archived", "climber_key"}


def read_notes(text) -> tuple[list[dict], dict]:
    """Beta's notes CSV -> ([{"cid", "day", "text", "author"}], counts).
    Archived notes, notes with no member id and notes with no readable date
    are left out (each counted). Raises when the export has no header or is
    missing a column we pair on: an empty answer must not turn every row on
    the tab into "no note"."""
    csv.field_size_limit(10 ** 8)                     # one very long note must not stop the step
    reader = csv.DictReader(io.StringIO(str(text or "").lstrip("\ufeff")))
    cols = set(reader.fieldnames or [])
    if not NOTE_COLUMNS <= cols:
        raise RuntimeError("the notes export is empty or missing column(s) "
                           f"{sorted(NOTE_COLUMNS - cols)}; the tabs were left as they were")
    out, st = [], {"read": 0, "archived": 0, "no_member": 0, "no_date": 0}
    for r in reader:
        st["read"] += 1
        if str(r.get("archived") or "").strip().lower() not in ("", "false", "0"):
            st["archived"] += 1
            continue
        cid = str(r.get("climber_key") or "").strip()
        if not cid:
            st["no_member"] += 1
            continue
        day = _note_day(r.get("issue_time"))
        if day is None:
            st["no_date"] += 1
            continue
        out.append({"cid": cid, "day": day, "text": " ".join(str(r.get("note") or "").split()),
                    "author": str(r.get("author") or "").strip()})
    st["usable"] = len(out)
    return out, st


# --------------------------------------------------------------------------
# SHIFT (Beta): cancelled plan rows, pairing, counts
# --------------------------------------------------------------------------

def beta_cancelled_plans(memberships_csv, today: date) -> tuple[list[dict], list[dict]]:
    """(one dict per cancelled plan row, the site's episodes).

    A row counts when the site's own reader keeps it (so staff plans and
    freeze-fee rows are out) and its cancel date is on or before today.
      came_back = the person's membership ran on past this plan (another plan
                  already running, or one started within SWITCH_GAP days).
      counted   = this row ended the membership AND the site counts the person
                  as a cancellation for that month (gym_metrics._beta_flows)."""
    mem_rows = gm._rows(memberships_csv)
    eps, _skipped = gm._beta_episodes(mem_rows, today)
    by_cid: dict[str, list[dict]] = defaultdict(list)
    for e in eps:
        by_cid[e["cid"]].append(e)
    out, seen = [], set()
    for r in mem_rows:
        one, _s = gm._beta_episodes([r], today)       # the site's own row filter
        if not one or one[0]["end"] is None:
            continue
        cid, start, end = one[0]["cid"], one[0]["start"], one[0]["end"]
        mine = by_cid.get(cid, [])
        ep = next((e for e in mine if e["start"] <= start
                   and (e["end"] is None or e["end"] >= end)), None)
        came_back = ep is not None and (ep["end"] is None or ep["end"] > end)
        m = _month_start(end)
        m_end = min(_next_month(m) - timedelta(days=1), today)
        # one "yes" per person per cancel day (two plans ending together are
        # one cancellation on the site), so the yes rows add up to its number
        counted = ((not came_back) and (cid, end) not in seen
                   and gm._beta_flows(mine, m, m_end)[1] == 1)
        if counted:
            seen.add((cid, end))
        out.append({"cid": cid, "plan": _plan_name(r.get("prices")), "start": start,
                    "cancel": end, "came_back": came_back, "counted": counted, "notes": []})
    out.sort(key=lambda x: (x["cancel"], x["cid"], x["start"]))
    return out, eps


def pair(plans: list[dict], notes: list[dict], since: date = DETAIL_SINCE) -> dict:
    """Attach to every cancelled plan from `since` each note on the same
    member written within NOTE_WINDOW_DAYS of the cancel date. Returns the
    sanity counts (notes paired / left over)."""
    by_cid: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    for i, n in enumerate(notes):
        by_cid[n["cid"]].append((i, n))
    used = set()
    for p in plans:
        p["notes"] = []
        if p["cancel"] < since:
            continue
        for i, n in by_cid.get(p["cid"], []):
            if abs((n["day"] - p["cancel"]).days) <= NOTE_WINDOW_DAYS:
                p["notes"].append(n)
                used.add(i)
        p["notes"].sort(key=lambda n: n["day"])
    return {"notes": len(notes), "paired": len(used), "left_over": len(notes) - len(used)}


def detail_rows(plans: list[dict], gym: str, stamp: str, since: date = DETAIL_SINCE) -> list[list]:
    out = []
    for p in plans:
        if p["cancel"] < since:
            continue
        ns = p["notes"]
        text = " | ".join(n["text"] or "(blank note)" for n in ns)
        if len(text) > NOTE_CELL_MAX:
            text = text[:NOTE_CELL_MAX] + " ... (cut, see Beta)"
        out.append([gym, p["cid"], p["plan"], p["start"].isoformat(), p["cancel"].isoformat(),
                    _yn(p["came_back"]), _yn(p["counted"]),
                    text if ns else NO_NOTE,
                    " | ".join(n["author"] for n in ns),
                    " | ".join(n["day"].isoformat() for n in ns), stamp])
    return out


def beta_counts(plans: list[dict], eps: list[dict], today: date, gym: str, stamp: str,
                since: date = DETAIL_SINCE) -> list[list]:
    """SHIFT's rows for the counts tab. The cancel columns cover each month
    whole; the note columns count the detail tab's rows (cancels from `since`)."""
    out = []
    for m in _months(COUNTS_FIRST_MONTH, today):
        m_end = min(_next_month(m) - timedelta(days=1), today)
        mine = [p for p in plans if m <= p["cancel"] <= m_end]
        people = gm._beta_flows(eps, m, m_end)[1]     # the site's number
        noted = sum(1 for p in mine if p["cancel"] >= since and p["notes"])
        out.append([gym, _label(m, today), len(mine), sum(1 for p in mine if p["came_back"]),
                    people, noted, round(noted / len(mine), 4) if mine else "", stamp])
    return out


# --------------------------------------------------------------------------
# ABC (RGP): counts from the daily status history
# --------------------------------------------------------------------------

def rgp_counts(history: list[dict], today: date, gym: str, stamp: str) -> list[list]:
    """ABC's rows for the counts tab, months the status history covers whole.
    Cancelled plans = every drop from OK / FROZEN to anything else in the
    month; Cancellations (people) = gym_metrics._rgp_left (the site's number);
    Came back = the drops that function leaves out. The note columns stay blank."""
    days = [d for d in (gm._d(r.get("date")) for r in history) if d is not None]
    if not days:
        raise RuntimeError("no member status history (file missing or empty)")
    first = min(days)
    by_person: dict[str, list[tuple[date, int, str]]] = defaultdict(list)
    for i, r in enumerate(history):
        d = gm._d(r.get("date"))
        if d is not None:
            by_person[r["climber_key"]].append((d, i, r["status"]))
    drops: list[date] = []
    for evs in by_person.values():
        evs.sort()                                    # the same order _rgp_left walks
        for j in range(1, len(evs)):
            if gm._active_like(evs[j - 1][2]) and not gm._active_like(evs[j][2]):
                drops.append(evs[j][0])
    out = []
    for m in _months(COUNTS_FIRST_MONTH, today):
        if first > m:                                 # month not covered whole: no number
            continue
        m_end = min(_next_month(m) - timedelta(days=1), today)
        n_drops = sum(1 for d in drops if m <= d <= m_end)
        people = len(gm._rgp_left(history, m, m_end)[0])
        out.append([gym, _label(m, today), n_drops, max(0, n_drops - people), people, "", "", stamp])
    return out


# --------------------------------------------------------------------------
# the sheet
# --------------------------------------------------------------------------

def _a1(tab: str, rng: str) -> str:
    return f"'{tab}'!{rng}"


def _hex(h: str) -> dict:
    h = h.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}


def _tab_ids(svc, sheet_id: str, need: list[str]) -> dict:
    def read():
        meta = svc.spreadsheets().get(
            spreadsheetId=sheet_id,
            fields="sheets(properties(sheetId,title))").execute(num_retries=_NUM_RETRIES)
        return {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta.get("sheets", [])}
    ids = read()
    add = [t for t in need if t not in ids]
    if add:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": t}}} for t in add]}
            ).execute(num_retries=_NUM_RETRIES)
        ids = read()
    return ids


def _write_grid(svc, sheet_id: str, tab: str, grid: list[list], last_col: str) -> None:
    """Write from A1, then clear whatever sat below the new last row (so the
    tab is never left empty between a clear and a write)."""
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=_a1(tab, "A1"), valueInputOption="RAW",
        body={"values": grid}).execute(num_retries=_NUM_RETRIES)
    svc.spreadsheets().values().clear(
        spreadsheetId=sheet_id,
        range=_a1(tab, f"A{len(grid) + 1}:{last_col}")).execute(num_retries=_NUM_RETRIES)


def _fmt(tab_id: int, ncols: int, percent_col: int | None, wrap_col: int | None) -> list:
    reqs = [
        {"updateSheetProperties": {
            "properties": {"sheetId": tab_id, "gridProperties": {"frozenRowCount": 2}},
            "fields": "gridProperties.frozenRowCount"}},
        {"repeatCell": {
            "range": {"sheetId": tab_id, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {"wrapStrategy": "OVERFLOW_CELL", "textFormat": {
                "italic": True, "foregroundColor": _hex("#7f8c8d")}}},
            "fields": "userEnteredFormat(wrapStrategy,textFormat)"}},
        {"repeatCell": {
            "range": {"sheetId": tab_id, "startRowIndex": 1, "endRowIndex": 2,
                      "startColumnIndex": 0, "endColumnIndex": ncols},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
            "fields": "userEnteredFormat.textFormat"}},
    ]
    if percent_col is not None:
        reqs.append({"repeatCell": {
            "range": {"sheetId": tab_id, "startRowIndex": 2,
                      "startColumnIndex": percent_col, "endColumnIndex": percent_col + 1},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "PERCENT", "pattern": "0%"}}},
            "fields": "userEnteredFormat.numberFormat"}})
    if wrap_col is not None:
        reqs += [
            {"repeatCell": {
                "range": {"sheetId": tab_id, "startRowIndex": 2,
                          "startColumnIndex": wrap_col, "endColumnIndex": wrap_col + 1},
                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP"}},
                "fields": "userEnteredFormat.wrapStrategy"}},
            {"updateDimensionProperties": {
                "range": {"sheetId": tab_id, "dimension": "COLUMNS",
                          "startIndex": wrap_col, "endIndex": wrap_col + 1},
                "properties": {"pixelSize": 480}, "fields": "pixelSize"}},
        ]
    return reqs


def write_counts(svc, sheet_id: str, gym: str, rows: list[list]) -> int:
    """Replace this gym's rows on the counts tab, keep every other gym's.
    Returns how many other-gym rows were kept."""
    ids = _tab_ids(svc, sheet_id, [COUNTS_TAB])
    n = len(COUNTS_HEADER)
    got = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=_a1(COUNTS_TAB, "A3:H2000"),
        valueRenderOption="UNFORMATTED_VALUE").execute(num_retries=_NUM_RETRIES).get("values", [])
    kept = [(list(r) + [""] * n)[:n] for r in got
            if r and str(r[0]).strip() and str(r[0]).strip().upper() != gym.upper()]
    body = sorted(kept + rows, key=lambda r: (str(r[1])[:7], str(r[0])))
    _write_grid(svc, sheet_id, COUNTS_TAB, [[COUNTS_NOTE], COUNTS_HEADER] + body, "H")
    svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": _fmt(ids[COUNTS_TAB], n, COUNTS_HEADER.index("Share with a note"), None)}
        ).execute(num_retries=_NUM_RETRIES)
    return len(kept)


def write_detail(svc, sheet_id: str, rows: list[list]) -> None:
    """Rewrite the detail tab whole (SHIFT only)."""
    ids = _tab_ids(svc, sheet_id, [DETAIL_TAB])
    _write_grid(svc, sheet_id, DETAIL_TAB, [[DETAIL_NOTE], DETAIL_HEADER] + rows, "K")
    svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": _fmt(ids[DETAIL_TAB], len(DETAIL_HEADER), None, DETAIL_HEADER.index("Note"))}
        ).execute(num_retries=_NUM_RETRIES)


def _svc():
    from . import drive_io
    return drive_io.sheets_service()


def _stamp() -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(GYM_TZ)).strftime("%Y-%m-%d %H:%M ET")
    except Exception:                                 # noqa: BLE001
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# --------------------------------------------------------------------------
# the two pull steps (each caller wraps its call in a guard, project rule 5)
# --------------------------------------------------------------------------

def shift_run(notes_text, memberships_csv, today: date, *, write: bool, gym: str = "SHIFT",
              svc=None, sheet_id: str | None = None, stamp: str | None = None) -> dict:
    """SHIFT's morning step: pair notes with cancels, print the count line (no
    note text, no names), and with write=True rewrite both tabs. Raises on any
    problem; the caller prints `cancel notes FAILED`."""
    stamp = stamp or _stamp()
    notes, st = read_notes(notes_text)
    if not notes:
        # Beta has held notes since 2025-06: none at all means a bad download
        raise RuntimeError(f"the notes export has no usable note ({st}); "
                           "the tabs were left as they were")
    plans, eps = beta_cancelled_plans(memberships_csv, today)
    if not plans:
        raise RuntimeError("the memberships file has no cancelled plan at all "
                           "(missing or unreadable); the tabs were left as they were")
    paired = pair(plans, notes)
    detail = detail_rows(plans, gym, stamp)
    counts = beta_counts(plans, eps, today, gym, stamp)
    with_note = sum(1 for r in detail if r[DETAIL_HEADER.index("Note")] != NO_NOTE)
    share = f" ({round(100 * with_note / len(detail))}%)" if detail else ""
    print(f"  cancel notes: {len(detail)} cancelled plans since {DETAIL_SINCE.isoformat()}, "
          f"{with_note} with a note{share}")
    res = {"detail": detail, "counts": counts, "with_note": with_note,
           "notes": st, "paired": paired, "written": False}
    if write:
        svc = svc or _svc()
        sheet_id = sheet_id or ALLGYMS_SHEET_ID
        write_detail(svc, sheet_id, detail)
        res["kept"] = write_counts(svc, sheet_id, gym, counts)
        res["written"] = True
        print(f"  All Gyms sheet: '{DETAIL_TAB}' {len(detail)} rows, '{COUNTS_TAB}' "
              f"{len(counts)} {gym} month(s) written, {res['kept']} other gym row(s) kept")
    else:
        print("  (All Gyms sheet not written on this run)")
    return res


def abc_run(history_csv, today: date, *, write: bool, gym: str = "ABC",
            svc=None, sheet_id: str | None = None, stamp: str | None = None) -> dict:
    """ABC's morning step: this gym's rows on the counts tab from the member
    status history. Raises on any problem; the caller prints `cancel counts FAILED`."""
    stamp = stamp or _stamp()
    counts = rgp_counts(gm.load_history(history_csv), today, gym, stamp)
    for r in counts:
        print(f"  cancel counts: {r[1]}: {r[2]} membership(s) stopped, {r[3]} came back "
              f"within a month, {r[4]} cancellation(s)")
    if not counts:
        # the history began 2026-10-16, so November 2026 on is always covered:
        # no month at all means the history file was lost or re-seeded
        raise RuntimeError("no month is covered whole by the status history (file lost "
                           "or re-seeded?); this gym's rows were left as they were")
    res = {"counts": counts, "written": False}
    if write:
        res["kept"] = write_counts(svc or _svc(), sheet_id or ALLGYMS_SHEET_ID, gym, counts)
        res["written"] = True
        print(f"  All Gyms sheet: '{COUNTS_TAB}' {len(counts)} {gym} month(s) written, "
              f"{res['kept']} other gym row(s) kept")
    else:
        print("  (All Gyms sheet not written on this run)")
    return res
