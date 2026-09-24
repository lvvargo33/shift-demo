"""One set of reporting definitions for both gyms (ROADMAP Block 15, 2026-09-24).

Why this exists. Before 2026-09-24 the All Gyms sheet, the SHIFT site and the
ABC site each carried their own copy of "joined", "came back", "clicked" and
"which section an email belongs to", and the copies had drifted (the audit in
Block 15 found "joined" computed five ways on the SHIFT site alone). Luke's
ruling: ONE set of definitions everywhere, and every number that appears in
two places comes from the same code. The rules live here; the sheet writer
(allgyms_push), the scorecard (engine.build_scorecard) and, from Block 15
session 3, the site's tiles and Insights call these functions instead of their
own copies.

The rules, in plain words (the sheet's Definitions tab says the same):
  * Joined = the first priced membership that is NOT a youth plan, created
    within 30, 60 or 90 days of the clock start (same day counts). Youth joins
    never count anywhere (Luke 2026-09-24). REPORTING ONLY: the sender keeps
    using ingest's membership_created, because a youth member's family must
    still be treated as converted and stop getting offers.
  * Came back = a check-in on a later calendar day within 30 days of the
    clock start. Scorecard clock = the first paid visit; email clock = the
    email (attribution.py applies the email side, strictly after the send).
  * Maturity = a rate only counts people / emails whose window has passed;
    counts are never cut.
  * Clicked = a click on a link in the exact email sent, survey form links,
    buy links and the /s survey links (the in-email rating buttons) included.

Pure functions, no I/O. Shared by both gyms (CLAUDE.md rule 4): keep this
file byte-identical in ABC/Automation and SHIFT/SHIFT_Automation.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

# --- windows (days) ---------------------------------------------------------
RETURN_DAYS = 30        # came back
JOIN_DAYS_SHORT = 30    # joined, quick read (Luke 2026-09-24: 30 / 60 / 90 everywhere)
JOIN_DAYS = 60          # joined, first read (the email credit window)
JOIN_DAYS_LONG = 90     # joined, the scorecard's headline window
ANSWER_DAYS = 14        # answered the survey
REDEEM_DAYS = 30        # redeemed the offer

# A membership plan whose every part names one of these words is a youth plan
# (SHIFT: "Youth Unlimited Recurring Monthly Membership", "Monthly Recurring -
# (Youth 12 & Under)"; ABC: "One Month Prepaid - Youth", "Youth (1) Month
# Membership"). A family row that also carries an adult plan is NOT youth.
YOUTH_KEYWORDS = ("youth",)

# --- clicks -------------------------------------------------------------------
# Survey links: the Google Form, and every link routed through a gym's /s
# endpoint (the rating buttons in the survey email, and every survey link once
# the native survey page is live). Before 2026-09-24 the /s links were missed
# on the sheet and on both sites (audit M7).
SURVEY_LINK_MARKERS = ("docs.google.com/forms", "forms.gle",
                       "shift-live-dash.onrender.com/s?",
                       "appalachian-live-dash.onrender.com/s?")
# SHIFT's offer emails carry a buy link; ABC's offers are redeemed at the desk
# and carry none (ABC's config can add markers, see engine).
BUY_LINK_MARKERS = ("purchase-a-pass", "sendmoregetbeta")
# The engagement cache's click_rule column: "2" = the click was measured on
# THIS email (campaign / template) with the markers above. A blank or older
# value is re-measured once and the new answer replaces the old flag.
CLICK_RULE = "2"
# "kept" = the re-measure could not see this email any more (Mailchimp's feed
# holds a person's newest 50 events; Brevo keeps 90 days), so the older flag
# was kept rather than wiped. Settled either way: never asked again.
CLICK_RULE_KEPT = "kept"
CLICK_SETTLED = (CLICK_RULE, CLICK_RULE_KEPT)


def _d(s) -> date | None:
    if isinstance(s, date):
        return s
    try:
        return date.fromisoformat(str(s or "")[:10])
    except ValueError:
        return None


# --- joined -------------------------------------------------------------------

def is_youth_plan(plan: str, keywords: tuple = YOUTH_KEYWORDS) -> bool:
    """True when EVERY part of a membership's plan text names a youth plan.
    Parts are split on "|" and ";" (a Beta row can list several prices). An
    empty plan is not youth (an unnamed membership still counts as a join)."""
    parts = [p.strip().lower() for p in re.split(r"[|;]", plan or "") if p.strip()]
    return bool(parts) and all(any(k in p for k in keywords) for p in parts)


def first_join(plans) -> str | None:
    """The reporting join date: the earliest (date, plan text) whose plan is
    not a youth plan, as YYYY-MM-DD, or None. Undated rows are skipped."""
    best = None
    for when, plan in plans or []:
        d = _d(when)
        if d is None or is_youth_plan(plan):
            continue
        if best is None or d < best:
            best = d
    return best.isoformat() if best else None


def joined_within(join_date, start, days: int) -> bool:
    """Joined 0..days days after `start` (the same day counts)."""
    j, s = _d(join_date), _d(start)
    return j is not None and s is not None and 0 <= (j - s).days <= days


# --- came back ----------------------------------------------------------------

def came_back_within(visit_days, start, days: int = RETURN_DAYS) -> bool:
    """A check-in on a LATER calendar day, at most `days` days after `start`.
    Check-ins on or before `start` never count."""
    s = _d(start)
    if s is None:
        return False
    hi = s + timedelta(days=days)
    for v in visit_days or ():
        d = _d(v)
        if d is not None and s < d <= hi:
            return True
    return False


# --- maturity -------------------------------------------------------------------

def window_passed(start, days: int, asof) -> bool:
    """`days` whole days have elapsed since `start` as of `asof`, so an outcome
    with that window can be judged (a rate's bottom only counts these)."""
    s, a = _d(start), _d(asof)
    return s is not None and a is not None and (a - s).days >= days


# --- sections -------------------------------------------------------------------
# Order = the order the sheet's Dashboard and the site's email slides show them.
SECTIONS = ["FTV funnel emails", "Day pass regulars", "Blocker nudges", "Surveys"]


def section_of(trigger_name: str) -> str:
    """Which section of email results a trigger belongs to (sheet + site).
    member_survey = ABC's member program, one row under Surveys (Luke
    2026-09-10). Day-pass regulars are NOT first-time visitors (3-10 visits
    each), so they sit apart from the FTV funnel."""
    if trigger_name in ("survey_request", "survey_reminder", "member_survey"):
        return "Surveys"
    if trigger_name.startswith("nudge_") and trigger_name != "nudge_round_two":
        return "Blocker nudges"
    if trigger_name == "daypass_to_trial":
        return "Day pass regulars"
    return "FTV funnel emails"
