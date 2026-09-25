"""The All Gyms sheet's email results, shown on each gym's Send It site
(ROADMAP Block 15, session 3, 2026-09-24).

Why this exists. Luke's ruling (2026-09-24): ONE set of definitions, and the
sheet's email results go ONTO the site, built once. Until now the site's
Insights "Survey engagement" and "Offer email results" slides counted in their
own way (per person, first touch, no maturity) while the sheet counts per
email, last touch, windowed, mature-only rates (audit M4-M6). Rather than a
second copy of the sheet's arithmetic, the site now READS the rows the sheet's
own writer produced (allgyms_push, the Data tab, this gym's "automation" rows)
and only groups them. Every number on the email slides is therefore the
sheet's number, as of the last 11 AM run.

Groups (the order the site shows them):
  * surveys     - the first-timer survey + its reminder (the sheet's "Surveys"
                  section minus ABC's member survey, which is not first-timer
                  outreach and has its own Members pages);
  * comeback    - come-back offers: the 50% offer, its reminder, round two and
                  the blocker emails (the sheet's "FTV funnel emails" minus the
                  membership + trial offers, plus "Blocker nudges");
  * membership  - membership + trial offers, judged on JOINS (they go to
                  people who already came back, so "came back" is the wrong
                  yardstick; Luke 2026-09-24);
  * regulars    - day-pass regulars (the sheet's "Day pass regulars").

I/O lives in `fetch_rows` only (one Sheets read, read-only scope); the rest is
pure. The web service calls `refresh()` from a background thread every few
minutes and hands `snapshot()` to engine.build_payload, so a page render never
waits on Google. Shared by both gyms (CLAUDE.md rule 4): keep this file
byte-identical in ABC/Automation and SHIFT/SHIFT_Automation.
"""
from __future__ import annotations

import os
import threading
import time

from . import definitions

SHEET_ID = os.getenv("ALLGYMS_SHEET_ID", "1s4Cg7vZbriq1PjDGLPkc8PZUr2ou-XKK3Hr3I0QFaHo")
DATA_RANGE = "Data!A2:BZ500"   # row 2 = header, rows 3+ = data (row 1 = the note)
_RO = "https://www.googleapis.com/auth/spreadsheets.readonly"
REFRESH_SECS = int(os.getenv("EMAIL_RESULTS_TTL", "600") or "600")

# client slug -> the Gym label its cron writes on the sheet. A slug that is not
# here (the demo clients) gets no email results, and the site keeps its older
# engagement slides.
GYM_OF_SLUG = {"shift": "SHIFT", "appalachian": "ABC"}

# The sheet's "Automation / email version" labels (allgyms_push.FUNNEL_LABELS
# in each tree) that belong to the membership + trial group, and the member
# program row. _verify_site_block15.py checks these against both trees' label
# tables, so a renamed label fails a test instead of moving a row silently.
MEMBERSHIP_LABELS = frozenset({
    "Membership offer (2-3d after 2nd visit)",
    "Membership offer, test arm A",
    "2-week trial offer, test arm B",
})
MEMBER_PROGRAM_LABELS = frozenset({"Member survey (every 180 days)"})

GROUPS = [
    ("surveys", "Survey emails"),
    ("comeback", "Come-back offers"),
    ("membership", "Membership and trial offers"),
    ("regulars", "Day-pass regulars"),
]

# sheet header -> key. Counts only: every rate is recomputed from counts after
# the group is summed (the sheet's own % cells cannot be added up). The
# "Mature ..." headers are the machine-only columns right of GymOrder.
HEADERS = {
    "Sends": "sends", "Delivered": "delivered", "Opens": "opens",
    "Link clicks": "clicks", "Unsubscribes": "unsubs", "Responses": "responses",
    "Offer redemptions": "redeems", "Returned after send": "returned",
    "Joined within 30 days": "converted30", "Joined within 60 days": "converted",
    "Joined within 90 days": "converted90",
    "Mature sends (14 days)": "m_sends14", "Mature responses (14 days)": "m_resp14",
    "Mature sends (30 days)": "m_sends30", "Mature returned (30 days)": "m_ret30",
    "Mature joined (30 days)": "m_conv30",
    "Mature sends (60 days)": "m_sends60", "Mature joined (60 days)": "m_conv60",
    "Mature sends (90 days)": "m_sends90", "Mature joined (90 days)": "m_conv90",
}
COUNT_KEYS = tuple(HEADERS.values()) + ("deliv_den",)
# Gyms whose writer divides Open % by Sends when Delivered is blank / 0 (ABC,
# Brevo receipts expire); SHIFT's writer divides by Delivered only.
DELIVERED_FALLBACK = frozenset({"ABC"})


class LayoutError(ValueError):
    """The Data tab lacks columns the site needs (old layout or a rename)."""

    def __init__(self, missing: list):
        super().__init__(f"Data tab is missing {len(missing)} column(s): {missing[:4]}")
        self.missing = missing


def group_of(section: str, label: str) -> str | None:
    """Which site group a sheet row belongs to (None = not first-timer
    outreach, e.g. ABC's member survey)."""
    if label in MEMBER_PROGRAM_LABELS:
        return None
    if section == "Surveys":
        return "surveys"
    if section == "Day pass regulars":
        return "regulars"
    if label in MEMBERSHIP_LABELS:
        return "membership"
    if section in ("FTV funnel emails", "Blocker nudges"):
        return "comeback"
    return None


def _num(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def parse_rows(values: list, gym: str) -> list[dict]:
    """The Data tab's values (header first) -> this gym's automation rows as
    {label, section, updated, <count keys>}. Missing columns read 0."""
    if not values:
        return []
    hdr = [str(h).strip() for h in values[0]]
    missing = [h for h in HEADERS if h not in hdr]
    if missing:
        # the sheet has not been rewritten by the Block 15 writer yet (or a
        # column was renamed): reading a missing column as 0 would print "no
        # email is 30 days old yet" and 0 joins, so refuse instead
        raise LayoutError(missing)
    idx = {h: i for i, h in enumerate(hdr) if h}

    def cell(r, h):
        i = idx.get(h)
        return r[i] if i is not None and i < len(r) else ""

    out = []
    for r in values[1:]:
        if not r or str(cell(r, "Gym")).strip() != gym:
            continue
        if str(cell(r, "Level")).strip() != "automation":
            continue
        row = {"label": str(cell(r, "Automation / email version")).strip(),
               "section": str(cell(r, "Section")).strip(),
               "updated": str(cell(r, "Updated")).strip()}
        for h, k in HEADERS.items():
            row[k] = _num(cell(r, h))
        # Open % bottom, per row, exactly as each gym's writer divides: the
        # delivered count when there is one, else the sends (ABC rows whose
        # Brevo receipts are gone read blank Delivered)
        row["deliv_den"] = ((row["delivered"] or row["sends"]) if gym in DELIVERED_FALLBACK
                            else row["delivered"])
        out.append(row)
    return out


def build(rows: list[dict], gym: str) -> dict:
    """Group this gym's sheet rows for the site. Every group carries summed
    counts; the page computes each rate from them the way the sheet does
    (outcome rates from the mature counts only)."""
    groups = {k: {"key": k, "label": lbl, "emails": [],
                  **{c: 0 for c in COUNT_KEYS}} for k, lbl in GROUPS}
    for r in rows:
        g = group_of(r["section"], r["label"])
        if g is None:
            continue
        gr = groups[g]
        if r["sends"]:
            gr["emails"].append(r["label"])  # the emails that went out, for the caption
        for c in COUNT_KEYS:
            gr[c] += r[c]
    stamps = sorted({r["updated"] for r in rows if r["updated"]})
    return {
        "available": bool(rows),
        "gym": gym,
        "updated": stamps[-1] if stamps else "",
        "windows": {"answer": definitions.ANSWER_DAYS, "return": definitions.RETURN_DAYS,
                    "join": [definitions.JOIN_DAYS_SHORT, definitions.JOIN_DAYS,
                             definitions.JOIN_DAYS_LONG],
                    "redeem": definitions.REDEEM_DAYS},
        "groups": [groups[k] for k, _l in GROUPS],
    }


def fetch_rows(gym: str, svc=None) -> list[dict]:
    """One read-only Sheets read of the Data tab -> this gym's rows."""
    if svc is None:
        from . import drive_io
        svc = drive_io.sheets_service(_RO)
    values = svc.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=DATA_RANGE,
        valueRenderOption="UNFORMATTED_VALUE").execute(num_retries=5).get("values", [])
    return parse_rows(values, gym)


# --- the web service's cached copy -----------------------------------------------
_lock = threading.Lock()
_state: dict = {}          # slug -> {"payload": dict, "at": monotonic}


def refresh(slug: str) -> dict | None:
    """Re-read the sheet for this client. Keeps the last good copy when the
    read fails (the error prints, the page keeps its numbers)."""
    gym = GYM_OF_SLUG.get(slug)
    if gym is None:
        return None
    try:
        payload = build(fetch_rows(gym), gym)
    except LayoutError as exc:
        print(f"  email results: {exc}; the page says the sheet is not updated yet")
        payload = {"available": False, "gym": gym, "groups": [], "reason": "layout"}
    except Exception as exc:  # noqa: BLE001 (a Sheets blip must never 500 the page)
        print(f"  email results: sheet read FAILED ({type(exc).__name__}: {exc}); "
              f"keeping the last copy")
        return snapshot(slug)
    with _lock:
        _state[slug] = {"payload": payload, "at": time.monotonic()}
    return payload


def snapshot(slug: str) -> dict | None:
    """What the page shows: the last good read, a not-available stub while the
    first read has not landed (or failed), None for a client with no row on
    the sheet (demo clients: the page keeps its older slides)."""
    gym = GYM_OF_SLUG.get(slug)
    if gym is None:
        return None
    with _lock:
        ent = _state.get(slug)
    if ent is None:
        return {"available": False, "gym": gym, "groups": []}
    return ent["payload"]


def refresher(slug: str) -> None:
    """Background loop for the web service (daemon thread). The Sheets service
    is built once for this thread (drive_io.sheets_service caches it)."""
    if slug not in GYM_OF_SLUG:
        return
    while True:
        refresh(slug)
        time.sleep(REFRESH_SECS)
