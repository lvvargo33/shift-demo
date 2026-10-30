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
  * Group visit = a check-in on a day when one purchase bought 10+ day
    passes, arriving with the group (5+ unpaid first-timers within 30
    minutes), by someone who had not paid their own way in (Block 16,
    2026-09-28): no emails, out of the email results and the scorecard; a
    fresh first-timer once they pay their own way in.

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
# on the sheet and on both sites (audit M7). "/survey-tap?token=" = the rating
# buttons in SHIFT's Brevo survey emails (ROADMAP Block 25 A1, 2026-10-23).
SURVEY_LINK_MARKERS = ("docs.google.com/forms", "forms.gle",
                       "shift-live-dash.onrender.com/s?",
                       "appalachian-live-dash.onrender.com/s?",
                       "/survey-tap?token=")
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


# --- group visits (ROADMAP Block 16, 2026-09-29) ----------------------------------
# A school trip or party: one purchase buys a stack of day passes and the kids
# check in on passes they did not buy. On 2026-09-17 one purchase of 68 SHIFT
# day passes sent 66 students the survey, the day-5 offer and the reminder.
# Luke's rules (2026-09-28): a GROUP VISIT is a check-in on a day when one
# purchase bought GROUP_MIN_PASSES+ day passes, by someone who had not paid
# their own way in (2026-09-29: a snack does not count, see is_own_entry);
# group visitors get no emails and leave the email results and the
# scorecard; one who later pays their own way in is a fresh first-timer from
# that visit.
GROUP_MIN_PASSES = 10
# A group arrives together (Luke 2026-09-29): an unpaid first check-in on a
# group day counts only when GROUP_BURST_PEOPLE+ such first-timers (itself
# included) checked in within GROUP_BURST_MINUTES of it. SHIFT's pilot groups
# arrived 10-30 at a time inside ~10 minutes; the unpaid walk-ins the day-only
# rule also caught (a friend's guest at 2:53 PM on a 7 PM school day) came alone.
GROUP_BURST_PEOPLE = 5
GROUP_BURST_MINUTES = 30

_TIME_RE = re.compile(r"[T ](\d{1,2}):(\d{2})")


def minute_of_day(ts) -> int | None:
    """'2026-09-17T08:17:45' / '2026-09-17 08:17:45 -0400' -> 497, else None."""
    m = _TIME_RE.search(str(ts or ""))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def burst_members(times: dict, people: int = GROUP_BURST_PEOPLE,
                  minutes: int = GROUP_BURST_MINUTES) -> set:
    """The keys of `times` (id -> first check-in minute that day, or None)
    that arrived with a group: at least `people` of them, this one included,
    checked in within `minutes` of it. A check-in with no readable time is
    judged on the whole day (it matches every other one)."""
    out = set()
    for k, t in times.items():
        n = sum(1 for u in times.values()
                if t is None or u is None or abs(u - t) <= minutes)
        if n >= people:
            out.add(k)
    return out


def group_pass_count(lines, words, exclude_words=()) -> int:
    """Day passes bought in ONE purchase: the summed quantity of its line
    items (qty, name) whose name holds one of `words` and none of
    `exclude_words` (lowercase). Several day-pass lines in one purchase add
    up (a 13 + 20 is one purchase of 33)."""
    n = 0
    for qty, name in lines or ():
        s = (name or "").lower()
        if any(w in s for w in words) and not any(x in s for x in exclude_words):
            n += int(qty or 0)
    return n


def is_own_entry(names, words, exclude_words=(), entry_words=()) -> bool:
    """True when one of a purchase's line names (lowercase) paid the buyer's
    OWN way in: a day pass (a `words` hit with no `exclude_words` hit, the
    same test as group_pass_count) or an `entry_words` hit (trial, punch
    pass, membership). A snack, gear or a rental does not (Luke 2026-09-29:
    three 9/17 students bought a latte or cookies on the school's passes)."""
    for name in names or ():
        s = (name or "").lower()
        if any(w in s for w in entry_words):
            return True
        if any(w in s for w in words) and not any(x in s for x in exclude_words):
            return True
    return False


def split_group_days(visit_days, bulk_days, own_buy_days):
    """-> (kept, group): a climber's check-in days split into the days that
    count and the days that do not. `own_buy_days` = the days they paid their
    own way in (an is_own_entry purchase, a member / trial / punch pass at
    check-in, a membership start). A climber is a group visitor only when
    their FIRST check-in was on a bulk day with no own entry on or before it.
    Then every check-in before their first own-entry day moves out (a later
    free visit as someone's guest does not start the clock either, fresh-eyes
    2026-09-29), and the clock starts on that paid day. Anyone else keeps
    every day, bulk day or not, so a member or a regular never loses one."""
    days = sorted(set(visit_days or ()))
    first_buy = min(own_buy_days) if own_buy_days else None
    if not days or days[0] not in bulk_days or (
            first_buy is not None and first_buy <= days[0]):
        return set(days), set()
    group = {d for d in days if first_buy is None or d < first_buy}
    return set(days) - group, group


def group_send(group_visit_days, visit_days, sent) -> bool:
    """True when a group visit triggered this send: the climber's first
    check-ins were group visits and the email went out on or before their
    first paid visit of their own (or they never paid their own way). Those
    sends leave the sheet and the site; the outreach log keeps them."""
    if not group_visit_days:
        return False
    own_first = min(visit_days) if visit_days else None
    return own_first is None or str(sent or "")[:10] <= own_first


# --- first-timer types (ROADMAP Block 27, Luke 2026-10-26, built 2026-10-28) ------
# Chris (10/25): segment adult first-timers. Luke's rulings: the Pilot scorecard
# stays on qualified first-timers everywhere (the 2025 comparison and the goals
# are built on it); the All Gyms Scorecard tab gets a "First-timer type" picker;
# the Home page's first-timer card shows All. A person's clock under a type
# starts on ONE day, chosen from three dates engine._ftv_classifier finds:
#   q  = the qualified first paid visit (today's scorecard rule, unchanged);
#   g  = the first visit as a guest: own profile, entry paid by someone else (a
#        member's guest pass, a pass a friend bought), before any paid entry of
#        their own and before any qualified date, the same email screen as
#        qualified; youth passes, groups, staff and family on someone else's
#        plan stay out;
#   d1 = the first visit of a day-one join / trial starter: a membership (or a
#        trial after the visit) within the scorecard's converted_within_days of
#        the first paid visit. They never get the first-timer emails, so they
#        are not qualified; All counts them (Chris: the gym's new-customer
#        acquisition as a whole).
# Nobody is counted twice across a guest visit and a later paid visit
# (decision 3): the guest visit came first, so it wins in the combined types.
# The Pilot scorecard keeps today's rule (that later paid visit is a new
# qualified first-timer), so its numbers never move.
FTV_TYPES = ("qualified", "guests", "qualified_guests", "all")
FTV_TYPE_LABELS = {
    "qualified": "Qualified",
    "guests": "Guests",
    "qualified_guests": "Qualified + guests",
    "all": "All",
}
# A guest visit on one of these passes is a program, a class or a youth
# entry, not a first-time visitor in Chris's sense (decision 7: comps, events,
# groups and youth passes stay out of every type). Matched on the pass text of
# the first check-in (SHIFT's Beta export: "Middle/High Meetup", "Day Pass -
# Youth", "Homeschool Membership", "Shift Sessions"); ABC's check-ins carry no
# pass text, so this never fires there. A check-in that carries an EVENT
# (Beta's `event` column: a comp, a summer camp, a homeschool session) is out
# too. Found on the 2026-10-22 cut (Block 27 build): 15 event check-ins and 5
# meetup passes among SHIFT's 240 pilot "guests".
GUEST_PASS_EXCLUDE_WORDS = ("youth", "meetup", "homeschool", "camp", "class", "lesson",
                            "sessions", "staff", "spectator", "birthday", "party")
FTV_TYPE_DEFAULT = "qualified"     # the Scorecard tab's picker on a fresh sheet
FTV_TYPE_HOME = "all"              # the Home page first-timer card (decision 1)
FTV_NO_GOAL = "no goal set"        # the goal cells of every type but Qualified
# The Home card's title and the one small line under it (decision 8).
FTV_HOME_TITLE = "All first-time visitors"
FTV_HOME_NOTE = ("Includes guests and day-one joins. The pilot scorecard counts "
                 "qualified first-timers only.")


def ftv_type_label(ftv_type: str) -> str:
    return FTV_TYPE_LABELS.get(ftv_type, ftv_type)


def ftv_type_start(ftv_type: str, q=None, g=None, d1=None) -> str | None:
    """The day a person's clock starts under `ftv_type` (YYYY-MM-DD), or None
    when they are not in that type. q / g / d1 as above. The guest visit is
    the earliest of the three whenever it exists (engine sets g only before
    any paid entry), so it wins in the two combined types."""
    if ftv_type == "qualified":
        return q or None
    if ftv_type == "guests":
        return g or None
    if ftv_type == "qualified_guests":
        return g or q or None
    if ftv_type == "all":
        return g or q or d1 or None
    raise ValueError(f"unknown first-timer type {ftv_type!r}")


# --- sections -------------------------------------------------------------------
# Order = the order the sheet's Dashboard and the site's email slides show them.
SECTIONS = ["FTV funnel emails", "Day pass regulars", "Blocker nudges", "Surveys"]


def section_of(trigger_name: str) -> str:
    """Which section of email results a trigger belongs to (sheet + site).
    member_survey = ABC's member program, one row under Surveys (Luke
    2026-09-10); member_check_in = SHIFT's day-14 new-member email, next to it
    (Luke 2026-10-26). Day-pass regulars are NOT first-time visitors (3-10
    visits each), so they sit apart from the FTV funnel."""
    if trigger_name in ("survey_request", "survey_reminder", "member_survey", "member_check_in"):
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
    from .gym_metrics import SWITCH_GAP as gm_gap, WINDOW_DAYS as gm_window
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
                 "Is the pilot moving qualified first-timers? How many came back "
                 "and joined, next to the same dates a year earlier.",
                 "Per qualified first-timer. The clock starts on their first paid "
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
        ("row", ["Qualified first-timer",
                 "First paid entry was a day pass or a trial (SHIFT: Day Pass, "
                 "2-Week Trial, 30-Day Trial; ABC: day use). Their very first "
                 "purchase was not a youth item, not staff, checked in at "
                 "least once, first paid visit inside the dates on the "
                 "Scorecard tab, and would have passed the email screens: own "
                 "email on file, email not shared with another climber, no "
                 "membership of any kind (youth included, as for sending) and "
                 "no trial bought within 2 days.",
                 "", "Pilot scorecard"]),
        ("row", ["Group visit",
                 f"A school trip or party: a check-in on a day when one "
                 f"purchase bought {GROUP_MIN_PASSES} or more day passes, "
                 f"arriving with the group ({GROUP_BURST_PEOPLE} or more such "
                 f"first-timers checked in within {GROUP_BURST_MINUTES} "
                 f"minutes), by someone who had not paid their own way in on or before "
                 f"that day (a day pass, trial, punch pass or membership of "
                 f"their own; a snack or a rental does not count). Group "
                 f"visitors get no emails and are left out of the email "
                 f"results and the scorecard, and so are the emails a group "
                 f"visit set off before this rule (from 2026-09-29); their "
                 f"survey answers still show with the other answers. Someone "
                 f"who later pays their own way in is a new first-timer from "
                 f"that visit.",
                 "", "Both"]),
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
        # Block 27 (2026-10-28): the Scorecard tab's "First-timer type" picker
        # and the Home page card. Chris's 10/25 segments, Luke's 10/26 rulings.
        ("band", ["First-timer types: the Scorecard tab's picker, and the Home "
                  "page's first-timer card"]),
        ("header", ["Type", "Who counts", "The clock starts on", "Where you see it"]),
        ("row", [FTV_TYPE_LABELS["qualified"],
                 "The Pilot scorecard's group, unchanged (see 'Qualified "
                 "first-timer' above). The only type with a goal: the 2025 "
                 "comparison and the goals are built on it.",
                 "Their first paid visit",
                 "Scorecard tab (the picker's default), the Pilot scorecard "
                 "slide, the Dashboard tiles and Insights on each gym's page"]),
        ("row", [FTV_TYPE_LABELS["guests"],
                 "Someone with their own profile whose first visit was paid for "
                 "by someone else: a member's guest pass, or a pass a friend "
                 "bought (SHIFT: the check-in names the host, or is on a "
                 "single-visit pass they did not buy; ABC: no purchase of their "
                 "own on or before that day). Same email screen as qualified: "
                 "own email on file, not shared with another climber. Out: "
                 "youth passes, school and party groups, staff, family on "
                 "someone else's plan, and anyone who had paid their own way in "
                 "before. Counted once, on that first guest visit, even if they "
                 "later buy their own pass; the Pilot scorecard still counts "
                 "that later paid visit as a new qualified first-timer, so its "
                 "numbers do not move. ABC's check-ins carry no pass type, so a "
                 "child on a guest pass cannot be told apart there.",
                 "Their first visit as a guest",
                 "Scorecard tab"]),
        ("row", [FTV_TYPE_LABELS["qualified_guests"],
                 "Both groups together. A person counts once: a guest who later "
                 "paid their own way in counts on the guest visit only.",
                 "The guest visit, else the first paid visit",
                 "Scorecard tab"]),
        ("row", [FTV_TYPE_LABELS["all"],
                 "Qualified + guests, plus people who joined or bought a trial "
                 "on their first visit (the same 2-day screen the qualified "
                 "rule uses). They never get the first-timer emails, so they are "
                 "not qualified; All shows the gym's new-customer acquisition "
                 "as a whole. Still out: comps, event entries, group visits, "
                 "staff, youth passes, family on someone else's plan.",
                 "The guest visit, else the first paid visit",
                 f"Scorecard tab; the Home page's '{FTV_HOME_TITLE}' card"]),
        ("row", ["Goals and 2025 for each type",
                 "Every type gets its own 'same dates a year earlier' row, "
                 f"counted the same way. The goal is set on {FTV_TYPE_LABELS['qualified']} "
                 f"only; the other types say '{FTV_NO_GOAL}'.",
                 "", "Scorecard tab"]),
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
        ("row", ["Day-pass regulars email",
                 "Two or more day passes bought in 30 days, not a member; a "
                 "membership offer, once per person.",
                 "Three or more check-in days in 30 days, a day pass of their "
                 "own at least once, never a member (incl. RGP's member "
                 "list), no email of any kind in the last 30 days; a "
                 "membership email, at most every 90 days.",
                 "Different rules for the same idea. The sheet's 'Day pass to "
                 "trial' row and its all-gyms total add both; compare inside "
                 "one gym."]),
        ("row", ["Survey formats",
                 "Rating buttons in the email (Q1 taps) and link emails.",
                 "Link emails. ABC also surveys current members (the 'Member "
                 "survey' row: an answer counts with no time limit, for every "
                 "member-survey email sent before it, and the row has no "
                 "came-back or join numbers).",
                 "Q1 taps are SHIFT only."]),
        # Block 23 (2026-10-16): the Home page Gym card. Luke: keep definitions
        # as consistent as possible, and say where the card differs from the
        # pilot's "Joined" (it answers a GM's question, not the pilot's).
        # Block 24 (2026-10-16 call): a period switch and arrows on the card.
        # Block 29 (2026-10-28): + average member lifespan (a today number).
        ("band", [f"Home page Gym card: five numbers for the GM, this month so far "
                  f"or the last {gm_window} days"]),
        ("header", ["Term", "What it means", "Window", "Used in"]),
        ("row", ["Member count",
                 "People with a membership running today, paying. Frozen members "
                 "are not in the number; the card shows them on a line under it. "
                 "SHIFT: a family or dual plan counts once (the account holder); "
                 "staff plans and freeze fees are left out. ABC: RGP members with "
                 "status OK, so everyone on a family plan counts. Its arrow "
                 f"compares with the same date last month (or {gm_window} days "
                 "ago); at ABC that earlier count comes from the weekly member "
                 "count until the daily status record reaches back that far.",
                 "Today", "Each gym's Home page"]),
        ("row", ["Avg member lifespan",
                 "The plain average time since joining, in months, for the "
                 "people in Member count (frozen left out). The clock starts at "
                 f"the member's current stretch: a plan switch within {gm_gap} "
                 "days keeps it running, a comeback after more than "
                 f"{gm_gap} days away starts it again. SHIFT: Beta's membership "
                 "start dates. ABC: the member's own membership purchases on "
                 "RGP invoices (the New members rule); a member with no "
                 "purchase of their own (a family member on someone else's "
                 f"plan), or whose own purchases ran out more than {gm_gap} "
                 "days ago, is left out (their start cannot be seen), and the "
                 "card says how many were dated. RGP shows who paid, not who "
                 "was covered, so a gap in a member's own purchases does not "
                 "restart the clock when their check-ins show they kept "
                 "climbing through it (2+ visit days a month, no break over 2 "
                 "months, under a quarter of those visits paid as day passes). "
                 "Someone who joined before the gym's data begins counts from "
                 "that first day. No arrow.",
                 "Today", "Each gym's Home page"]),
        ("row", ["Period and arrows",
                 "The Home page shows this month so far (the default), compared "
                 f"with the same days last month, or the last {gm_window} days, "
                 f"compared with the {gm_window} days before. The period ends on "
                 "the last day with sales in the data (pulled each morning). "
                 "Green arrow = better, red = worse (for cancellations, fewer is "
                 "better); hovering shows the % change. An earlier period with "
                 "no number (ABC cancellations before 2026-10-16) has no arrow.",
                 f"This month so far / last {gm_window} days", "Each gym's Home page"]),
        ("row", ["New members",
                 f"People who became members: a first membership, or a comeback "
                 f"more than {gm_gap} days after their last one ended. Counted "
                 f"per person, so switching plans within {gm_gap} days is not a "
                 f"new member, and a late renewal is not either. Youth plans "
                 f"count. Different from 'Joined' above on purpose: Joined asks "
                 f"whether a first-timer converted, so it leaves youth plans out "
                 f"and only counts a first-ever join; this asks how many members "
                 f"the gym added. SHIFT: Beta's membership start dates. ABC: "
                 f"membership purchases on RGP invoices, counted for the person "
                 f"who paid (a family plan once).",
                 "The Home period", "Each gym's Home page"]),
        ("row", ["Cancellations",
                 f"People who stopped being members: their last membership ended "
                 f"and they held none at the end of the period. A plan switch (a new plan within "
                 f"{gm_gap} days) is not a cancellation. Freezing is not a "
                 f"cancellation. SHIFT: Beta's cancel date, which may be the day "
                 f"the cancel was entered rather than the last day of access. "
                 f"ABC: RGP keeps no cancel dates, so Send It writes down every "
                 f"member's status each morning and counts from the day that "
                 f"record began (2026-10-16).",
                 "The Home period", "Each gym's Home page"]),
        ("row", ["Net sales",
                 "Everything sold and paid for, before sales tax, with refunds "
                 "taken off. Gift card sales are left out "
                 "(the visit counts when the card is spent). SHIFT: counted the "
                 "way Beta's sales report counts them (checked against SHIFT's "
                 "September 2026 report): a sale on the day it was paid, a refund "
                 "on the day it was made; unpaid (open) and cancelled sales are "
                 "left out. ABC: RGP invoice lines on the invoice day; voided invoices and "
                 "bookkeeping lines are left out. Send It adds these up itself "
                 "because Beta's own revenue dashboard is not reliable.",
                 "The Home period", "Each gym's Home page"]),
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
