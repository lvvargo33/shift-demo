"""Insights chart builder: the per-person table (ROADMAP Block 28, step 1).

One packed row per first-timer CLOCK, with no identity in it: no name, no
email, no customer id, no birthday (Luke 2026-11-10: "rows with no identity").
The builder page filters and groups these rows in the browser, so any mix of
filters works and every total adds up.

SHARED MODULE: byte-identical in the ABC and SHIFT trees (project rule 4).
A column a gym's export does not carry (ABC: no birthday, no online flag)
ships as "Not known" for everyone and the dim says `available: false` with
the reason, so the page can grey it out instead of drawing a one-bar chart.

Who is a row
------------
A person's clock under a first-timer type starts on ONE day
(definitions.ftv_type_start, from the three dates engine._ftv_classifier
finds). Most people have one clock. A guest who later pays their own way in
has two: the guest day (types Guests / Qualified + guests / All) and the
later paid day (type Qualified, the Pilot scorecard's rule). Each distinct
clock day is its own row, and `types` says which types that row belongs to,
so picking a type never counts anyone twice.

The same people as today's page, proven by _verify_insights_rows.py:
  rows with the Qualified bit == engine.build_reporting()["ftvs"]
  rows with the All bit       == engine.build_reporting()["ftvs_all"]

Outcomes ship as DAY COUNTS from the clock day, not dates:
  r2 = days to the first check-in on a later day (None = not yet)
  j  = days to the reporting join date (definitions.first_join; None = no)
so "came back within 30 days" is 1 <= r2 <= 30 and "joined within N days" is
0 <= j <= N, the same arithmetic as definitions.came_back_within /
joined_within. The clock day itself stays an exact date (as on today's
page): the period picker and the "has had N days" rule need the day.

Pure read + shape. No network. Reads the client's transactions and sessions
CSVs a second time for the columns ingest does not keep (purchase channel,
discount, birthday, check-in time, guest_of).
"""
from __future__ import annotations

import csv
from datetime import date

from . import definitions, ingest
from .config import ClientConfig

VERSION = 1

# Row layout. A code column holds an index into dims[<col>]["values"].
COLS = ("d", "types", "kind", "cat", "nva", "r2", "j",
        "wd", "tod", "age", "party", "price", "chan", "email", "intent", "blocker")
CODE_COLS = ("kind", "cat", "wd", "tod", "age", "party", "price", "chan",
             "email", "intent", "blocker")

TYPE_BITS = {t: 1 << i for i, t in enumerate(definitions.FTV_TYPES)}

UNKNOWN = "Not known"
KINDS = ("Qualified first-timer", "Guest", "Joined on day one")
WEEK_PARTS = ("Weekday", "Weekend")
# Morning = before noon, afternoon = noon to 4:59 PM, evening = 5 PM on
# (the gym's local clock, as the check-in export writes it).
DAY_PARTS = ("Morning", "Afternoon", "Evening", UNKNOWN)
AGE_BANDS = (("Under 18", 0, 17), ("18 to 24", 18, 24), ("25 to 34", 25, 34),
             ("35 to 44", 35, 44), ("45 and up", 45, 130))
PARTY = ("Came with others", "Came alone, as far as the data shows", UNKNOWN)
PRICE = ("Full price", "Discount", UNKNOWN)
CHANNEL = ("Online", "At the desk", UNKNOWN)
NO_EMAIL = "No email before coming back"
NO_ANSWER = "Did not answer the survey"
NO_ENTRY = "No purchase of their own"

DIM_LABELS = {
    "kind": "First-timer type",
    "cat": "Entry product",
    "wd": "First visit: weekday or weekend",
    "tod": "First visit: time of day",
    "age": "Age group",
    "party": "Came alone or with others",
    "price": "Price paid",
    "chan": "Bought online or at the desk",
    "email": "Last email before coming back",
    "intent": "Survey: how likely to come back",
    "blocker": "Survey: what held them back",
}
# Export sources that mean "bought online" (Beta: widget = the online store,
# pos = the front desk; a subscription renewal is neither).
ONLINE_SOURCES = {"widget": 0, "pos": 1}
# Birthdays the export fills in when the customer gave none.
PLACEHOLDER_DOBS = frozenset({"2000-01-01", "1900-01-01", "1970-01-01"})


class _Codes:
    """value -> index, in first-seen order after any fixed leading values."""

    def __init__(self, fixed=()):
        self.values = list(fixed)
        self._ix = {v: i for i, v in enumerate(self.values)}

    def code(self, value) -> int:
        v = value if value not in (None, "") else UNKNOWN
        i = self._ix.get(v)
        if i is None:
            i = self._ix[v] = len(self.values)
            self.values.append(v)
        return i


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b[:10]) - date.fromisoformat(a[:10])).days


def _age_band(dob: str, on: str) -> str | None:
    dob = (dob or "").strip()[:10]
    if not dob or dob in PLACEHOLDER_DOBS:
        return None
    try:
        b, d = date.fromisoformat(dob), date.fromisoformat(on[:10])
    except ValueError:
        return None
    age = d.year - b.year - ((d.month, d.day) < (b.month, b.day))
    for label, lo, hi in AGE_BANDS:
        if lo <= age <= hi:
            return label
    return None


def _clocks(ds, client, classify, opening) -> list:
    """[(climber, clock day, type bits, kind index)], one per distinct clock
    day. With no scorecard (demo clients) every person in build_reporting's
    wider group gets one Qualified-bit row, so the page still has data."""
    out = []
    if classify is None:
        excluded = set((client.reporting or {}).get("exclude_from_cohort", []))
        for c in ds.climbers.values():
            fd = c.ftv_date
            if (not fd or c.is_staff or c.ftv_category in excluded
                    or (opening and fd < opening) or c.visit_count < 1):
                continue
            out.append((c, fd, TYPE_BITS["qualified"], 0))
        return out
    for c in ds.climbers.values():
        dates = classify(c)
        by_day: dict[str, int] = {}
        for t in definitions.FTV_TYPES:
            s = definitions.ftv_type_start(t, **dates)
            if s and not (opening and s < opening):
                by_day[s] = by_day.get(s, 0) | TYPE_BITS[t]
        for s, bits in by_day.items():
            kind = 0 if s == dates["q"] else 1 if s == dates["g"] else 2
            out.append((c, s, bits, kind))
    return out


def _tx_facts(client: ClientConfig, wanted: dict, words, excl) -> tuple:
    """Second read of transactions for the columns ingest drops.
    wanted = {climber_id: ftv_time} (the entry purchase's sort key).
    -> ({cid: {"source", "discount", "passes"}}, {cid: dob}, has_source,
        has_discount, has_dob)"""
    entry: dict[str, dict] = {}
    dob: dict[str, str] = {}
    has = {"source": False, "discount_total": False, "climber_dob": False}
    with open(client.transactions_csv, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for k in has:
            has[k] = k in (reader.fieldnames or [])
        for row in reader:
            if row.get("state") != "SUCCEEDED":
                continue
            cid = (row.get("climber_id") or "").strip()
            if not cid or cid not in wanted:
                continue
            if has["climber_dob"] and cid not in dob:
                b = (row.get("climber_dob") or "").strip()[:10]
                if b and b not in PLACEHOLDER_DOBS:
                    dob[cid] = b
            if cid in entry or (row.get("time") or "")[:19] != wanted[cid]:
                continue
            lines = [(int(m.group(1)), m.group(2))
                     for m in map(ingest._LINE_RE.match, (row.get("items") or "").split(";")) if m]
            try:
                disc = float(row.get("discount_total") or 0) if has["discount_total"] else None
            except ValueError:
                disc = None
            entry[cid] = {
                "source": (row.get("source") or "").strip().lower() if has["source"] else None,
                "discount": disc,
                "passes": definitions.group_pass_count(lines, words, excl) if words else None,
            }
    return entry, dob, has["source"], has["discount_total"], has["climber_dob"]


def _session_facts(client: ClientConfig, wanted: set) -> tuple:
    """Second read of check-ins: the first check-in minute and the guest_of
    flag on each wanted (climber_id, day). -> ({key: minute}, {key}, has_guest)"""
    minute: dict[tuple, int] = {}
    guest: set = set()
    has_guest = False
    with open(client.sessions_csv, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        has_guest = "guest_of" in (reader.fieldnames or [])
        for row in reader:
            key = ((row.get("climber_key") or "").strip(), (row.get("entry") or "")[:10])
            if key not in wanted:
                continue
            m = definitions.minute_of_day(row.get("entry"))
            if m is not None and m < minute.get(key, 24 * 60):
                minute[key] = m
            if has_guest and (row.get("guest_of") or "").strip():
                guest.add(key)
    return minute, guest, has_guest


def _sends_by_person(sent_rows, non_ftv) -> tuple:
    """Real first-timer sends, by climber id and by email: [(day, tag)]."""
    by_id: dict[str, list] = {}
    by_email: dict[str, list] = {}
    for r in sent_rows or []:
        if (r.get("mode") or "").strip() == "test":
            continue
        if (r.get("trigger_name") or "").strip() in non_ftv:
            continue
        day = (r.get("sent_date") or "").strip()[:10]
        tag = (r.get("tag") or "").strip() or (r.get("trigger_name") or "").strip()
        if not day or not tag:
            continue
        cid = (r.get("climber_id") or "").strip()
        em = (r.get("email") or "").strip().lower()
        if cid:
            by_id.setdefault(cid, []).append((day, tag))
        if em:
            by_email.setdefault(em, []).append((day, tag))
    return by_id, by_email


def _last_email(sends: list, start: str, back_day: str | None) -> str | None:
    """The last email sent on/after the clock day and BEFORE the day they
    came back (any day, when they have not come back). Last-touch: one email
    per person, the same idea as the email results' one credit per person."""
    best = None
    for day, tag in sends:
        if day < start or (back_day is not None and day >= back_day):
            continue
        if best is None or day >= best[0]:
            best = (day, tag)
    return best[1] if best else None


def build(ds, client: ClientConfig, asof: date, sent_rows: list | None = None) -> dict:
    """The payload's `insights` block. Never raises for a missing optional
    column; a client with no reporting config gets enabled:false."""
    from . import engine  # late: engine imports this module's caller

    rep = client.reporting or {}
    if not rep.get("ftv_qualifying_categories"):
        return {"enabled": False, "v": VERSION, "cols": list(COLS), "rows": [], "dims": {}}
    opening = rep.get("post_opening_date")
    classify = engine._ftv_classifier(ds, client)
    clocks = _clocks(ds, client, classify, opening)

    grp = rep.get("group_visit") or {}
    words = [w.lower() for w in grp.get("pass_words", []) if w]
    excl = [w.lower() for w in grp.get("exclude_words", []) if w]
    entry, dob, has_source, has_disc, has_dob = _tx_facts(
        client, {c.climber_id: c.ftv_time for c, _s, _b, _k in clocks if c.ftv_time}, words, excl)
    minute, guest_keys, has_guest = _session_facts(
        client, {(c.climber_id, s) for c, s, _b, _k in clocks})
    by_id, by_email = _sends_by_person(sent_rows, engine.NON_FTV_TRIGGERS)

    labels = rep.get("entry_product_labels", {})
    codes = {
        "kind": _Codes(KINDS), "cat": _Codes(), "wd": _Codes(WEEK_PARTS),
        "tod": _Codes(DAY_PARTS), "age": _Codes([b[0] for b in AGE_BANDS] + [UNKNOWN]),
        "party": _Codes(PARTY), "price": _Codes(PRICE), "chan": _Codes(CHANNEL),
        "email": _Codes([NO_EMAIL]), "intent": _Codes([NO_ANSWER]),
        "blocker": _Codes([NO_ANSWER]),
    }
    rows = []
    for c, s, bits, kind in clocks:
        own = c.ftv_date == s            # this clock IS their entry purchase day
        later = sorted(str(v)[:10] for v in c.visit_days if str(v)[:10] > s)
        back_day = later[0] if later else None
        jd = c.join_date
        e = entry.get(c.climber_id) if own else None

        m = minute.get((c.climber_id, s))
        tod = UNKNOWN if m is None else ("Morning" if m < 12 * 60 else
                                         "Afternoon" if m < 17 * 60 else "Evening")
        if kind == 1 or (c.climber_id, s) in guest_keys:
            party = PARTY[0]             # a guest came on someone else's pass
        elif e and e["passes"] is not None:
            party = PARTY[0] if e["passes"] >= 2 else PARTY[1]
        else:
            party = UNKNOWN
        price = (UNKNOWN if not e or e["discount"] is None
                 else "Discount" if e["discount"] > 0.005 else "Full price")
        chan = UNKNOWN
        if e and e["source"] in ONLINE_SOURCES:
            chan = CHANNEL[ONLINE_SOURCES[e["source"]]]
        sends = by_id.get(c.climber_id) or by_email.get((c.email or "").strip().lower()) or []
        answered = bool(getattr(c, "survey_answered", False))
        cat = (labels.get(c.ftv_category, c.ftv_category) if own and c.ftv_category
               else NO_ENTRY)
        rows.append([
            s,
            bits,
            kind,
            codes["cat"].code(cat),
            min(sum(1 for v in c.visit_days if str(v)[:10] >= s), 99),
            _days(s, back_day) if back_day else None,
            _days(s, jd) if jd and jd >= s else None,
            0 if date.fromisoformat(s).weekday() < 5 else 1,
            codes["tod"].code(tod),
            codes["age"].code(_age_band(dob.get(c.climber_id, ""), s)),
            codes["party"].code(party),
            codes["price"].code(price),
            codes["chan"].code(chan),
            codes["email"].code(_last_email(sends, s, back_day) or NO_EMAIL),
            codes["intent"].code((c.survey_intent or UNKNOWN) if answered else NO_ANSWER),
            codes["blocker"].code((c.survey_blocker or UNKNOWN) if answered else NO_ANSWER),
        ])
    rows.sort(key=lambda r: (r[0], r[1], r[2], r[3]))   # by day: no file order leaks through

    why = {
        "age": (has_dob, "This gym's purchase export has no birthday column."),
        "price": (has_disc, "This gym's purchase export does not say when a discount was used."),
        "chan": (has_source, "This gym's purchase export does not say whether a sale was online."),
        "party": (bool(words) or has_guest,
                  "This gym has no group-pass words set up and its check-ins carry no guest column."),
    }
    dims = {}
    for col in CODE_COLS:
        ok, note = why.get(col, (True, ""))
        dims[col] = {"label": DIM_LABELS[col], "values": codes[col].values,
                     "available": bool(ok), "note": "" if ok else note}
    if not has_guest:
        dims["party"]["note"] = ("Read from purchases only: 2 or more day passes in one purchase, "
                                 "or a visit as someone's guest. This gym's check-ins do not "
                                 "name the host.")
    data_end = ds.sessions_max_date[:10] if ds.sessions_max_date else asof.isoformat()
    sc = rep.get("scorecard") or {}
    return {
        "enabled": True,
        "v": VERSION,
        "as_of": asof.isoformat(),
        "data_through": data_end,
        "post_opening_date": opening,
        "scorecard_start": (sc.get("start") or None) if classify is not None else None,
        "types": [{"key": t, "bit": TYPE_BITS[t], "label": definitions.ftv_type_label(t)}
                  for t in definitions.FTV_TYPES] if classify is not None
                 else [{"key": "qualified", "bit": 1, "label": "First-timers"}],
        "windows": {"return_days": int(sc.get("return_days", definitions.RETURN_DAYS)),
                    "join_days": [definitions.JOIN_DAYS_SHORT,
                                  int(sc.get("join_early_days", definitions.JOIN_DAYS)),
                                  int(sc.get("join_days", definitions.JOIN_DAYS_LONG))]},
        "cols": list(COLS),
        "dims": dims,
        "rows": rows,
    }
