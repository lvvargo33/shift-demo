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


# --- the sheet's Definitions tab (Block 15 session 4, 2026-09-25) -----------------
# Chris's condition for the pilot-extension ask: one set of definitions, written
# down where he can check it. The words live HERE, next to the rules, and every
# window in them is read from the constants above, so the tab cannot say 30 days
# while the code counts 45. Both gyms' morning runs write the whole tab with the
# same text (the last run of the day wins; only the stamp differs). The SHIFT vs
# ABC rows describe the email tools each gym's gather code reads (allgyms_push),
# which Luke ruled are documented, not changed (2026-09-24).
DEF_TAB_COLS = 4


def tab_rows(stamp: str) -> list[tuple[str, list[str]]]:
    """The Definitions tab as (kind, cells) rows, kind in 'title', 'note',
    'band', 'header', 'row'. cells has at most DEF_TAB_COLS entries."""
    r, a = RETURN_DAYS, ANSWER_DAYS
    j = f"{JOIN_DAYS_SHORT}, {JOIN_DAYS} and {JOIN_DAYS_LONG}"
    rows: list[tuple[str, list[str]]] = [
        ("title", ["Definitions: how every number on this sheet and on both "
                   "gyms' Send It pages is counted"]),
        ("note", [f"Written by each gym's morning run from the same code that "
                  f"counts the numbers; the day counts below are read from "
                  f"the settings the code counts with. Do not edit: the next "
                  f"run rewrites this tab. Updated {stamp}."]),
        ("band", ["Two questions, one set of rules"]),
        ("header", ["Question", "What it answers", "Counted per", "Where you see it"]),
        ("row", ["Email results",
                 "Did this email work? What the people who got it did next.",
                 "Per email sent. The clock starts on the send date. Each "
                 "person's outcome counts once, for the last email they got "
                 "before it.",
                 "This sheet: Dashboard, Variants, History, Data. Each gym's "
                 "page: the email slides on Insights (surveys, come-back "
                 "offers, membership offers, day-pass regulars; a slide shows "
                 "once its emails have gone out). Those slides read this "
                 "sheet's Data tab, so they show the same numbers; they "
                 "change once a day, shortly after the morning run. ABC's "
                 "member survey is on this sheet only."]),
        ("row", ["Pilot scorecard",
                 "Is the pilot moving paid first-timers? How many came back "
                 "and joined, next to the same dates a year earlier.",
                 "Per paid first-timer. The clock starts on their first paid "
                 "visit.",
                 "This sheet: Scorecard. Each gym's page: the Pilot scorecard "
                 "slide, the Dashboard's first-timer tiles (on 'Since the "
                 "pilot started' they equal the slide) and the Insights "
                 "charts on return visits, entry product, conversion over "
                 "time and when members join."]),
        ("row", ["Why they differ",
                 "Someone who comes back before their first email has gone "
                 "out counts as 'came back' on the scorecard, but no email "
                 "gets the credit. So the two answer different questions on "
                 "the same rules, and their numbers are not meant to match.",
                 "", ""]),
        ("band", ["Terms"]),
        ("header", ["Term", "What it means", "Window", "Used in"]),
        ("row", ["Paid first-timer",
                 "First paid entry was a day pass or a trial (SHIFT: Day Pass, "
                 "2-Week Trial, 30-Day Trial; ABC: day use). Their very first "
                 "purchase was not a youth item, not staff, checked in at "
                 "least once, first paid visit inside the dates on the "
                 "Scorecard tab, and would have passed the email screens: own "
                 "email on file, email not shared with another climber, no "
                 "membership of any kind (youth included, as for sending) and "
                 "no trial bought within 2 days.",
                 "", "Pilot scorecard"]),
        ("row", ["Came back",
                 "A check-in on a later calendar day. Check-ins on or before "
                 "the day the clock starts never count.",
                 f"{r} days from the first paid visit (scorecard) or from the "
                 f"email (email results)",
                 "Both"]),
        ("row", ["Joined",
                 "The first paid membership that is not a youth plan (the "
                 "same day counts). A youth membership never counts as a "
                 "join: the child joins, not the person we emailed. A family "
                 "membership that includes an adult plan counts. A trial or a "
                 "punch pass is not a join. (Sending is stricter on purpose: "
                 "any membership, youth included, stops the emails.)",
                 f"{j} days, each in its own columns",
                 "Both"]),
        ("row", ["Answered the survey",
                 "A survey answer matched to the person, credited to the last "
                 "survey email before it.",
                 f"{a} days from the survey email",
                 "Email results"]),
        ("row", ["Opened",
                 "An open of the email, Apple Mail's automatic opens included "
                 "at both gyms (Apple loads every email for its users), so an "
                 "open is not proof of reading. The two gyms tie opens to the "
                 "email differently: see the table below.",
                 "Measured in the days after the send, then kept. A later "
                 "re-check can add an open, never remove one",
                 "Email results"]),
        ("row", ["Clicked",
                 "A click on a link in this exact email: survey links "
                 "(including the rating buttons and any survey link that "
                 "goes through the gym's own Send It web address) and, at "
                 "SHIFT, the buy links.",
                 "Measured in the days after the send, then kept. A later "
                 "re-check can add a click",
                 "Email results"]),
        ("row", ["Q1 taps (rating buttons)",
                 "A tap on one of the 1 to 5 rating buttons inside the survey "
                 "email, which answers the survey's first question (SHIFT "
                 "only for now: ABC's survey email is a link).",
                 "", "Email results"]),
        ("row", ["Unsubscribed",
                 "Used the unsubscribe link in this exact email. A person who "
                 "leaves counts once, on the email that made them leave, and "
                 "gets no later emails.",
                 "", "Email results"]),
        ("row", ["Offer redemption",
                 "Bought the offered day pass after the email. The two gyms "
                 "detect it differently: see the table below.",
                 f"A later day, within {REDEEM_DAYS} days of the email",
                 "Email results"]),
        ("row", ["Purchases after send",
                 "Bought anything after the email.",
                 "A later day, within 30 days of the email",
                 "Email results"]),
        ("row", ["Credit rule",
                 "Each person's outcome (came back, joined, answered, "
                 "redeemed, bought) counts once, for the LAST email they got "
                 "before it, and only inside that email's window. So adding "
                 "up an automation's rows never counts a person twice. "
                 "'Opened first' = that person had opened that email before "
                 "acting.",
                 "", "Email results"]),
        ("row", ["Which emails are in a %",
                 "A rate only counts the emails (or people) whose window has "
                 "closed, so a send from last week is never read as a miss. "
                 "The counts next to each rate include everyone. Hover a % "
                 "header for its exact rule.",
                 f"Response %: emails {a}+ days old. Return %: {r}+. "
                 f"Join %: {JOIN_DAYS_SHORT}+, {JOIN_DAYS}+ or "
                 f"{JOIN_DAYS_LONG}+. Scorecard: the same, counted from the "
                 f"first paid visit",
                 "Both"]),
        ("row", ["Same dates a year earlier",
                 "The scorecard's comparison row: the same calendar dates one "
                 "year earlier, the same screens, each person given the same "
                 "number of days as their twin date this year.",
                 "", "Pilot scorecard"]),
        ("row", ["Email sections",
                 f"This sheet groups emails as {', '.join(SECTIONS[:-1])} and "
                 f"{SECTIONS[-1]}. Each gym's page shows the same rows as four "
                 f"slides: surveys, come-back offers (blocker nudges "
                 f"included), membership and trial offers, and day-pass "
                 f"regulars. Membership, trial and day-pass-regular emails go "
                 f"to people who already came back, so the page judges them "
                 f"on joins, not on came back.",
                 "", "Email results"]),
        ("band", ["Where SHIFT and ABC differ: the email tools, not the rules"]),
        ("header", ["What", "SHIFT (Mailchimp, Beta)", "ABC (Brevo, Rock Gym Pro)",
                    "What it means when comparing"]),
        ("row", ["Delivered",
                 "Mailchimp's send confirmation.",
                 "Brevo's delivered receipt. Brevo keeps 90 days of receipts, "
                 "so a send with no receipt inside that window stays "
                 "uncounted. A row where Brevo had no delivery info at all "
                 "shows Delivered blank, and its Open % and Unsubscribe % are "
                 "out of Sends instead.",
                 "Close, but not the same event."]),
        ("row", ["Opens",
                 "Tied to the exact email that was sent.",
                 "Tied to the email's group, not the exact email: an open of "
                 "any survey email counts for each survey email sent to that "
                 "person before it, and an open of any other first-timer "
                 "email (come-back offer, reminder, membership offer, round "
                 "two, blocker emails) counts for each of those sent before "
                 "it. An open Brevo records without the email's name also "
                 "counts. No delivered receipt needed.",
                 "ABC's Open % reads high next to SHIFT's. Compare versions "
                 "inside one gym, not open rates across gyms."]),
        ("row", ["Clicks",
                 "Survey links, rating buttons and buy links, tied to the "
                 "exact email.",
                 "Survey links only (ABC's offers are redeemed at the desk and "
                 "carry no buy link), tied to the exact email. A click Brevo "
                 "records without the email's name still counts.",
                 "SHIFT counts more kinds of links."]),
        ("row", ["Unsubscribes",
                 "Tied to the exact email.",
                 "Tied to the exact email.",
                 "Same meaning."]),
        ("row", ["Offer redemption",
                 "A day pass bought at 50% or more off (Beta does not record "
                 "which coupon was used).",
                 "A day pass bought at $8.25 or less whose product name "
                 "carries the email-discount marker, on RGP invoices.",
                 "Same idea, different evidence."]),
        ("row", ["Joins come from",
                 "Beta's Memberships export (the date the membership was "
                 "created, and its plan).",
                 "RGP's membership purchase lines on invoices.",
                 "Same rule (non-youth, 30 / 60 / 90 days)."]),
        ("row", ["First paid visit products",
                 "Day Pass, 2-Week Trial, 30-Day Trial.",
                 "Day use only (ABC sells no trial). A first purchase that is "
                 "only a rental counts: someone else covered that entry.",
                 "SHIFT's scorecard has a day pass vs trial split; ABC's does "
                 "not."]),
        ("row", ["Survey formats",
                 "Rating buttons in the email (Q1 taps) and link emails.",
                 "Link emails. ABC also surveys current members (the 'Member "
                 "survey' row: an answer counts with no time limit, for every "
                 "member-survey email sent before it, and the row has no "
                 "came-back or join numbers).",
                 "Q1 taps are SHIFT only."]),
        ("band", ["Known limits"]),
        ("row", ["Event history",
                 "Mailchimp shows a person's newest 50 events and Brevo keeps "
                 "90 days. When a send can no longer be seen, what was "
                 "measured earlier is kept, never erased. So a few older "
                 "emails (mostly ABC's, past Brevo's 90 days when clicks were "
                 "re-measured in late September 2026) keep the older click "
                 "rule, where a click in a later email could count for an "
                 "earlier one.",
                 "", ""]),
        ("row", ["Older rules",
                 "Until 2026-09-24: joins were read at 60 days only and youth "
                 "joins counted, clicks were not tied to the email, and every "
                 "email sat in every %. The History tab is recounted on every "
                 "run with today's rules, so its old months use them too.",
                 "", ""]),
    ]
    return rows
