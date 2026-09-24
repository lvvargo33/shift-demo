"""Per-person, windowed, last-touch outcome credit (roadmap block 6, phase B).

Why this exists. Until 2026-09-14 every send took credit for anything the
person did later, so one person who got three emails and then joined counted
as three conversions (the All Gyms sheet showed ABC 11 conversions for 5 real
people, SHIFT 19 for 9). Now each person credits exactly ONE send per outcome:

  * walk the person's outcome events in date order (returns, the join, the
    survey answer, the redemption, the first purchase), skipping anything
    dated before their first email;
  * for each event, the candidate is the most recent send at or before it
    (last touch: Luke's decision 2, 2026-09-10; Chris agreed 2026-09-14);
  * the first event that falls INSIDE that send's window credits that send
    and the person is done for that outcome. An event outside every window
    credits nobody (a return 45 days after the only email is not the
    email's), and the search moves on to the person's next event, so a later
    send can still earn a later, closer event.

Beside every credit the caller also learns whether the credited email had
been OPENED before the event ("opened it first?", Luke's decision 1,
2026-09-14, after Chris asked to credit only opened emails: opened-only
would leave ABC 1 join of 5 to test on, so the open is reported, not
required). An opened send whose open date is unknown (cache rows written
before opened_at existed, or "unknown") counts as opened first: opens land
within days of the send and the event is after the send.

Comparators match the per-send rules the sheet used before, so a same-day
join still credits the same-day email (>=) while a same-day visit does not
(>): returned/redeemed/purchased strictly after the send date,
converted/responded on or after.

Pure functions, no I/O. Shared by both gyms (CLAUDE.md rule 4): keep this
file byte-identical in ABC/Automation and SHIFT/SHIFT_Automation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from . import definitions

# outcome -> days after the send inside which the event still credits it
# (the day counts live in definitions.py since 2026-09-24, Block 15, so the
# sheet, the site and the experiments cannot drift apart)
WINDOWS = {
    "returned": definitions.RETURN_DAYS,
    "converted": definitions.JOIN_DAYS,
    "responded": definitions.ANSWER_DAYS,
    "redeemed": definitions.REDEEM_DAYS,
    "purchased": 30,
}
# outcomes whose event may fall ON the send date (the rest need a later day)
_SAME_DAY_OK = frozenset({"converted", "responded"})
OUTCOMES = tuple(WINDOWS)


@dataclass
class Send:
    email: str
    sent: str            # YYYY-MM-DD
    tag: str
    trig: str = ""
    opened: bool = False
    opened_at: str = ""  # YYYY-MM-DD, "" (never / not yet dated) or "unknown"
    extra: dict = field(default_factory=dict)  # caller's own fields ride along

    @property
    def key(self) -> tuple:
        return (self.email, self.sent, self.tag)


def _d(s: str) -> date | None:
    try:
        return date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def opened_before(send: Send, event: str) -> bool:
    """Had this send been opened before the event date? Unknown open dates
    count as yes (see the module docstring)."""
    if not send.opened:
        return False
    oa = _d(send.opened_at)
    return oa is None or oa.isoformat() <= event


def empty_credit() -> dict:
    """The per-send result shape, all False."""
    out = {}
    for o in OUTCOMES:
        out[o] = False
        out[o + "_opened"] = False
    return out


def credit(sends: list[Send], events: dict[str, dict[str, list[str]]],
           windows: dict[str, int] | None = None) -> dict[tuple, dict]:
    """sends: every real send (test rows already removed). events: per email,
    per outcome, the person's event dates (any order, duplicates fine), e.g.
    {"a@x.com": {"returned": ["2026-08-03", ...], "converted": ["2026-08-20"]}}.
    Returns {send.key: {"returned": bool, "returned_opened": bool, ...}} with
    an entry for EVERY send, so callers can index without .get()."""
    windows = dict(WINDOWS, **(windows or {}))
    out: dict[tuple, dict] = {s.key: empty_credit() for s in sends}
    by_person: dict[str, list[Send]] = {}
    for s in sends:
        by_person.setdefault(s.email, []).append(s)
    for email, ss in by_person.items():
        ss = sorted(ss, key=lambda s: s.sent)
        first = ss[0].sent
        ev = events.get(email) or {}
        for outcome in OUTCOMES:
            same_day = outcome in _SAME_DAY_OK
            window = windows[outcome]
            dates = sorted({(x or "")[:10] for x in (ev.get(outcome) or []) if x})
            for e in dates:
                if not (e >= first if same_day else e > first):
                    continue  # before the first email: not ours
                before = [s for s in ss if (s.sent <= e if same_day else s.sent < e)]
                if not before:
                    continue
                last = before[-1]
                ed, sd = _d(e), _d(last.sent)
                if ed is None or sd is None:
                    continue
                if (ed - sd).days > window:
                    continue  # too long after the last email: nobody's credit
                out[last.key][outcome] = True
                out[last.key][outcome + "_opened"] = opened_before(last, e)
                break
    return out


def mature(sent: str, outcome: str, today: date,
           windows: dict[str, int] | None = None) -> bool:
    """A send has had its full window to produce `outcome` by `today`.
    Experiment stats count only mature sends (decision 4: a test reads
    "Not readable yet" rather than judging emails that have not had time)."""
    w = (windows or WINDOWS)[outcome]
    sd = _d(sent)
    return sd is not None and (today - sd).days >= w
