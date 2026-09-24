"""Push this gym's per-automation + per-variant stats to the shared
"Send It - All Gyms" Google Sheet (v2 layout, ALLGYMS_SPEC.md 2026-07-28).

Tabs written:
  Data      - machine-written long-format rows (this gym's rows replaced,
              other gyms' rows kept). Dashboard + Variants read from here.
  Dashboard - one table, all gyms together, gym filter dropdown (FILTER()
              formulas off Data), combined totals row, sections: FTV funnel
              emails / Blocker nudges / Surveys.
  Variants  - same shape, one row per exact A/B tag (subject arms, embed
              arms), same gym filter.
  History   - one row per gym per month: emails sent that month beside the
              running total, every month recounted on every run (2026-09-18);
              the current month rides as "(so far)".

Run locally:  py allgyms_push.py            (needs GSHEETS_SA_KEY/_B64/_JSON +
                                             Mailchimp creds from .env)
Cron:         called at the end of send_nudges.py, guarded so a stats failure
              can never fail the send run.

Measurement rules baked in (2026-07-28):
- outreach log rows with mode=test are ignored.
- opens/clicks come from each recipient's activity feed, opens pinned to the
  exact journey email that was sent (engine._sent_campaign_ids).
- link clicks = survey-form OR buy-link URLs (engine's markers).
- Q1 taps (embed test) count only when the tapping email actually HAS an
  embed-tagged send on/before the tap date, and only the FIRST tap per email
  counts. This filters Mailchimp's activation-time link checker and any
  security scanners that crawl all five buttons at once.
- offer redemptions = a day-pass bought at >= 50% off strictly after the send
  (ingest's discounted_daypass_dates; Beta never names the coupon).
- purchases after send = ANY SUCCEEDED transaction strictly after the send
  (passes, retail, gear rental, memberships).
- returned-after-send = any visit day strictly after that email's sent date;
  converted-after-send = membership_created on/after sent date.
- rows with sends < 30 carry a small-n flag: directional only.
"""
from __future__ import annotations

import csv
import os
import re
import sys
import time
import traceback
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from nudge_tool import config, drive_io, engine, ingest, survey
from nudge_tool.mailchimp_client import MailchimpClient
from nudge_tool import attribution, definitions, experiments
from nudge_tool.stage import reclaim, stage as _stage


def stage(label: str) -> None:
    """Progress marker on stderr, tagged 'stats' (see nudge_tool/stage.py)."""
    _stage(label, tag="stats")

GYM = "SHIFT"  # the ABC port sets "ABC"
ALLGYMS_SHEET_ID = os.getenv(
    "ALLGYMS_SHEET_ID", "1s4Cg7vZbriq1PjDGLPkc8PZUr2ou-XKK3Hr3I0QFaHo")
OUTREACH_DRIVE_ID = os.getenv(
    "OUTREACH_DRIVE_ID", "1SUUYeh_7DabmIl6Ae7bSFLxsjGBUbdbi")
SMALL_N = 30

# googleapiclient's own retry loop (transient 5xx/429, read timeouts, dropped
# TLS) only runs when execute() is told how many times to try; the default is 0,
# i.e. one attempt. On 2026-09-05 a Sheets 503 on an unretried request killed
# ABC's stats push (the send itself was fine, so the sheet silently sat a day
# stale). Every request in this module passes it. Matches nudge_tool/drive_io.py
# and nudge_tool/survey.py; see rule 1 in the project CLAUDE.md.
_NUM_RETRIES = 5

# --- engagement cache (2026-08-04) ------------------------------------------
# The run used to ask Mailchimp for an activity feed for EVERY person ever
# emailed, every day. That loop was 181s of a 204s run and grew ~5s a day with
# the outreach log, heading for a 20-minute job by spring.
#
# An email that went out weeks ago is settled: the overwhelming majority of
# opens land within 48 hours. So only sends inside the fresh window are
# re-measured; older ones keep the delivered/opened/clicked we already measured,
# frozen in this cache. Everything else on the scoreboard (returned, converted,
# redeemed, purchased, responded, tapped) is still recomputed from local data
# every run, because those genuinely keep changing.
#
# Cost of the tradeoff, stated plainly: an open that arrives more than
# ENGAGEMENT_FRESH_DAYS after the send is not counted. Frozen numbers can never
# drop, only miss a very late gain.
#
# WITHOUT ENGAGEMENT_CACHE_DRIVE_ID SET, NOTHING CHANGES: every send is measured
# live, exactly as before. Cron containers are wiped between runs, so the cache
# has to live on Drive; the SA cannot create Drive files (personal-Gmail quota,
# see drive_io), so the file is created by hand once and its id passed in here.
ENGAGEMENT_CACHE_DRIVE_ID = os.getenv("ENGAGEMENT_CACHE_DRIVE_ID", "").strip()
ENGAGEMENT_FRESH_DAYS = int(os.getenv("ENGAGEMENT_FRESH_DAYS", "14") or 14)
# opened_at (2026-09-14, Luke's decision 2 after Chris's "last email opened"
# ask): the date of the first open pinned to that send, "" when never opened,
# "unknown" when the send was opened but the ESP no longer returns the event.
# A row that is opened but undated is re-fetched ONCE to fill the date; a
# frozen flag is never downgraded by a later feed. Same rule in the ABC copy.
#
# open_kind (2026-09-17, shared cache layout with the ABC copy): at ABC it
# says whether an open was the person's own mail app ("real") or only a mail
# proxy such as Apple Mail privacy protection ("proxy"). SHIFT ALWAYS WRITES
# IT BLANK: Mailchimp's activity feed counts both as a plain open and does not
# say which kind it was. Nothing here reads it.
#
# unsubscribed (2026-09-18, Tasks step 1.3): 1 = the person used THIS email's
# unsubscribe link (Mailchimp's `unsub` activity pinned to the send by campaign
# id, exactly like an open; Brevo's `unsubscribed` event by tag / subject at
# ABC), 0 = measured and not, "" = not measured under this rule yet. A settled
# row with a blank value is re-fetched ONCE (Mailchimp has no hourly feed cap,
# so the whole backfill lands in one longer run); the answer always fills the
# column, so it is never asked twice, and a measured 1 is never downgraded.
# The address is already suppressed from every later send (engine rule 5), so
# this column is the count of WHICH email made people leave.
#
# click_rule (2026-09-24, ROADMAP Block 15): definitions.CLICK_RULE when
# `clicked` was measured on THIS email (Mailchimp campaign id, like an open)
# counting /s survey links too; blank = the older rule (a click in any email
# after the send, Google Form and buy links only). A settled row under the
# older rule is re-fetched ONCE and the new answer REPLACES the old flag (the
# one time a frozen click may go 1 -> 0: the old rule over-counted); after
# that the never-downgrade rule applies again.
CACHE_FIELDS = ["email", "sent_date", "tag", "delivered", "opened", "clicked",
                "opened_at", "open_kind", "unsubscribed", "click_rule",
                "frozen_at"]
OPENED_AT_UNKNOWN = "unknown"


def _flag_or_blank(v) -> bool | None:
    """A 0/1 cache cell -> False/True; a blank cell -> None (not measured)."""
    v = (v or "").strip()
    return None if v == "" else v == "1"


def _cell_or_blank(v: bool | None):
    return "" if v is None else int(v)

# trigger_name -> display label (falls back to the raw name)
FUNNEL_LABELS = {
    "survey_request": "FTV survey (day 1-2)",
    "survey_reminder": "Survey reminder (day 2-3 after the survey)",
    "first_visit_reengage": "Reengage 50% offer (day 5-7)",
    "nudge_round_two": "Round two (high intent)",
    "nudge_pricing": "Blocker: pricing",
    "nudge_crowding": "Blocker: crowding",
    "nudge_too_hard": "Blocker: too hard",
    "nudge_intimidating": "Blocker: intimidating",
    "nudge_frontdesk": "Blocker: front desk",
    "nudge_confusing": "Blocker: confusing",
    "nudge_routes": "Blocker: routes",
    "ftv_reminder": "Offer reminder (day 9-10)",
    "membership_offer": "Membership offer (2-3d after 2nd visit)",
    "membership_offer_control": "Membership offer, test arm A",
    "trial_offer": "2-week trial offer, test arm B",
    "comeback": "Trial win-back",
    "daypass_to_trial": "Membership Lite offer (0-14d after last day pass)",
    # ABC-only member-survey program; listed here so the label + section +
    # glossary stay identical across the two copies (2026-09-10)
    "member_survey": "Member survey (every 180 days)",
}
# Order here is the order the Dashboard renders sections in. KEEP THIS LIST
# IDENTICAL IN BOTH REPOS. Each gym's cron rebuilds the whole Dashboard from
# its OWN copy, and ABC's cron runs at 11:00 while SHIFT's runs at 11:03, so a
# section that exists in only one repo is written at 11:03 and erased the next
# morning at 11:00 (or vice versa) with no error anywhere.
# Since 2026-09-24 both the list and the trigger -> section rule live in
# nudge_tool/definitions.py (byte-identical in both repos), so the sheet and
# the site's email slides cannot disagree about which section an email is in.
DASH_SECTIONS = definitions.SECTIONS


def _section_of(trigger_name: str) -> str:
    return definitions.section_of(trigger_name)


# Only tags that are arms of a running/ran A/B test appear on Variants.
# tag -> (test section band, plain-language arm label)
#
# PRE-TEST ROWS (Chris comment on Variants!D5, 2026-08-03): both SHIFT
# automations went live BEFORE their subject test existed, sending one version
# under a single un-split tag (survey 103 sends 6/20-7/03, offer 34 sends
# 6/24-7/03; the arms start 7/06). Those sends land in the Dashboard's
# per-automation total but had no Variants row, so the arm column read 103 (and
# 34) short of the Dashboard. Listing the old tags here as their own labelled
# row makes each test block add up to its Dashboard number. They are NOT arms:
# the label says so, and experiment_tests() never references them, so the
# Experiments tab's A-vs-B auto-fill is untouched.
PRETEST_LABEL = "Before the test started (context only, one version)"
MEMBERSHIP_TEST_BAND = "Membership offer test (membership vs 2-week trial)"
OUT_OF_TEST_LABEL = "Not in the test (already had a trial, or out of area)"
# Rows that sit inside a test block but are NOT arms. The writer sorts these to
# the bottom of their block and greys them, so a reader comparing A against B
# is never invited to read them as a third variant. Keep in sync across repos:
# the shared writer references this tuple by name.
CONTEXT_LABELS = (PRETEST_LABEL, OUT_OF_TEST_LABEL)
TEST_ARMS = {
    "send_it_survey_request": (
        "Survey email test (2x2: subject x body)", PRETEST_LABEL),
    "first_visit_reengage": ("Offer email subject test", PRETEST_LABEL),
    "FTV_survey_subject_a": (
        "Survey email test (2x2: subject x body)", "Subject A + link to the survey"),
    "FTV_survey_subject_a_embed": (
        "Survey email test (2x2: subject x body)", "Subject A + rating buttons in the email"),
    "FTV_survey_subject_b": (
        "Survey email test (2x2: subject x body)", "Subject B + link to the survey"),
    "FTV_survey_subject_b_embed": (
        "Survey email test (2x2: subject x body)", "Subject B + rating buttons in the email"),
    "FTV_reengage_subject_a": ("Offer email subject test", "Offer subject A"),
    "FTV_reengage_subject_b": ("Offer email subject test", "Offer subject B"),
    # Membership vs 2-week trial (Chris 2026-08-09, live 2026-08-13). Unlike
    # the subject tests above, the two arms are two different TRIGGERS, because
    # only the trial-eligible half of the returners can be randomised (a second
    # trial cannot be sold to someone who already had one). The third row is
    # every returner who could not enter the test; it is labelled as context,
    # not as an arm, so nobody reads it as a third variant.
    "send_it_membership_offer_a": (
        MEMBERSHIP_TEST_BAND, "Membership offer (A)"),
    "send_it_trial_offer": (
        MEMBERSHIP_TEST_BAND, "2-week trial offer (B)"),
    "send_it_membership_offer": (
        MEMBERSHIP_TEST_BAND, OUT_OF_TEST_LABEL),
    # Survey reminder test (Tasks step 2.2, built 2026-09-23, gated off until
    # the swap morning). Arm A sends nothing, so only arm B has a row here;
    # A vs B is read on the Experiments tab (E-006, a bucket test).
    "FTV_survey_reminder_embed": (
        "Survey reminder test (no reminder vs one reminder 2 days later)",
        "Reminder sent (arm B)"),
}
# TEST_ARMS tags that get their zero row on Variants only once a send exists,
# so a test built ahead of its start morning does not show on Chris's sheet.
SEED_WHEN_SENT = {"FTV_survey_reminder_embed"}
EMBED_TAGS = {"FTV_survey_subject_a_embed", "FTV_survey_subject_b_embed"}

# S40: plain-language short names used inside Send It Test dropdown values
_EXP_SHORT = {"survey_request": "survey", "first_visit_reengage": "offer"}


def experiment_tests(client) -> dict:
    """This gym's live A/B tests for the Experiments tab (S40):
    dropdown name -> (arm A tag list, arm B tag list). Names MUST match the
    'Send It Test' column on the Lists tab, which setup_pm_tabs.py seeds from
    this same function, so the two can only drift if a test is added without
    re-running the seed (the cron then notes the unknown name and skips)."""
    tests: dict = {}
    for t in client.triggers:
        if not t.active:
            continue
        ab = getattr(t, "ab_tags", None) or {}
        body = getattr(t, "ab_body_tags", None) or {}
        short = _EXP_SHORT.get(t.name, t.name.replace("_", " "))
        if ab:
            if body:  # 2x2: a subject arm spans both of its body variants
                a = [body["A"]["A"], body["A"]["B"]]
                b = [body["B"]["A"], body["B"]["B"]]
            else:
                a, b = [ab["A"]], [ab["B"]]
            tests[f"{GYM} - {short} subject (A vs B)"] = (a, b)
        if body:  # body test: an arm spans both subjects
            tests[f"{GYM} - {short} embed (current vs embed)"] = (
                [body["A"]["A"], body["B"]["A"]],
                [body["A"]["B"], body["B"]["B"]])
    # Cross-trigger test (2026-08-13): the membership-vs-trial arms are two
    # separate triggers, not two tags on one, so the loop above cannot find
    # them. Listed only while BOTH arms are active; when the test ends, drop
    # one arm's active flag and the Experiments row freezes at its final
    # numbers, the same way a retired ab_tags test does.
    live = {t.name for t in client.triggers if t.active}
    if {"membership_offer_control", "trial_offer"} <= live:
        tests[f"{GYM} - membership offer (membership vs 2-week trial)"] = (
            ["send_it_membership_offer_a"], ["send_it_trial_offer"])
    return tests

METRIC_HEADERS = [
    "Sends", "Delivered", "Opens", "Open %", "Link clicks", "Clicks per open %",
    "Unsubscribes", "Unsubscribe %",
    "Q1 taps",
    "Responses", "Response %", "Offer redemptions", "Purchases after send",
    "Returned after send", "Return %", "Returned, opened first",
    "Return % of readers",
    "Joined within 30 days", "Join % (30 days)",
    "Joined within 60 days", "Join % (60 days)", "Joined within 60 days, opened first",
    "Join % of readers (60 days)",
    "Joined within 90 days", "Join % (90 days)",
    "Note"]
METRIC_KEYS = [
    "sends", "delivered", "opens", "open_pct", "clicks", "cpo_pct",
    "unsubs", "unsub_pct",
    "taps",
    "responses", "resp_pct", "redeems", "purchases",
    "returned", "return_pct", "ret_opened", "ret_opened_pct",
    "converted30", "conv30_pct",
    "converted", "conv_pct", "conv_opened", "conv_opened_pct",
    "converted90", "conv90_pct", "note"]
# Block 15 (2026-09-24): "joined" is shown at 30, 60 AND 90 days, clearly
# labelled (30 added the same day, Luke: the membership offer test already
# reads joins at 30 days). The four 60-day columns were "Converted after send", "Conversion
# %", "Converted, opened first" and "Conversion % of readers" until then; a
# row read back under the old heading keeps its value under the new one (the
# other gym's cron may still be on the old layout for a few minutes).
RENAMED_HEADERS = {
    "Converted after send": "Joined within 60 days",
    "Conversion %": "Join % (60 days)",
    "Converted, opened first": "Joined within 60 days, opened first",
    "Conversion % of readers": "Join % of readers (60 days)",
}

# Maturity (Block 15, decided 2026-09-24): a rate only counts the emails whose
# window has finished (came back 30 days, joined 60 / 90, answered 14), so a
# send from yesterday is never read as a miss. The counts beside each rate are
# NOT cut. Each rate's top and bottom are therefore their own counts, carried
# as extra machine-only columns at the far right of the Data tab (after
# GymOrder, outside every FILTER range), so the "All gyms" rows, the
# "Everything combined" formulas and the History tab add them up exactly as
# they add up everything else. key -> (Data header, window in days).
MATURE_KEYS = {
    "m_sends14": ("Mature sends (14 days)", definitions.ANSWER_DAYS),
    "m_resp14": ("Mature responses (14 days)", definitions.ANSWER_DAYS),
    "m_sends30": ("Mature sends (30 days)", definitions.RETURN_DAYS),
    "m_ret30": ("Mature returned (30 days)", definitions.RETURN_DAYS),
    "m_opens30": ("Mature opens (30 days)", definitions.RETURN_DAYS),
    "m_retop30": ("Mature returned, opened first (30 days)", definitions.RETURN_DAYS),
    "m_conv30": ("Mature joined (30 days)", definitions.JOIN_DAYS_SHORT),
    "m_sends60": ("Mature sends (60 days)", definitions.JOIN_DAYS),
    "m_conv60": ("Mature joined (60 days)", definitions.JOIN_DAYS),
    "m_opens60": ("Mature opens (60 days)", definitions.JOIN_DAYS),
    "m_convop60": ("Mature joined, opened first (60 days)", definitions.JOIN_DAYS),
    "m_sends90": ("Mature sends (90 days)", definitions.JOIN_DAYS_LONG),
    "m_conv90": ("Mature joined (90 days)", definitions.JOIN_DAYS_LONG),
}
_MAT_KEYS = tuple(MATURE_KEYS)


def _add_mature(b: dict, rec: dict, today) -> None:
    """Add one send's maturity counts to a bucket (a Data row, a History
    month). rec = a SEND_RECORDS entry. A member-survey send (ftv False) has
    no first-timer outcomes, so it only feeds the 14-day response pair."""
    sent = rec.get("sent") or ""
    cr = rec.get("credits") or {}
    if definitions.window_passed(sent, definitions.ANSWER_DAYS, today):
        b["m_sends14"] += 1
        b["m_resp14"] += bool(rec.get("responded"))
    if not rec.get("ftv", True):
        return
    opened = bool(rec.get("opened"))
    if definitions.window_passed(sent, definitions.RETURN_DAYS, today):
        b["m_sends30"] += 1
        b["m_ret30"] += bool(cr.get("returned"))
        b["m_opens30"] += opened
        b["m_retop30"] += bool(cr.get("returned_opened"))
        b["m_conv30"] += bool(cr.get("converted30"))
    if definitions.window_passed(sent, definitions.JOIN_DAYS, today):
        b["m_sends60"] += 1
        b["m_conv60"] += bool(cr.get("converted"))
        b["m_opens60"] += opened
        b["m_convop60"] += bool(cr.get("converted_opened"))
    if definitions.window_passed(sent, definitions.JOIN_DAYS_LONG, today):
        b["m_sends90"] += 1
        b["m_conv90"] += bool(cr.get("converted90"))


def _bucket_add(b: dict, rec: dict, today) -> None:
    """Add one send (a SEND_RECORDS entry) to a bucket of counts. The ONE
    place a send becomes numbers: both gyms' Data rows and the History tab
    call it, so a month on History and a row on the Dashboard can only differ
    by which sends they hold. A member-survey send (ftv False) counts on the
    send / delivered / open / click / unsubscribe / response columns only."""
    cr = rec.get("credits") or {}
    b["sends"] += 1
    b["delivered"] += bool(rec.get("delivered"))
    b["opens"] += bool(rec.get("opened"))
    b["clicks"] += bool(rec.get("clicked"))
    b["unsubs"] += bool(rec.get("unsubscribed"))
    b["taps"] += bool(rec.get("tapped"))
    b["responses"] += bool(rec.get("responded"))
    if rec.get("ftv", True):
        b["redeems"] += bool(cr.get("redeemed"))
        b["purchases"] += bool(cr.get("purchased"))
        b["returned"] += bool(cr.get("returned"))
        b["ret_opened"] += bool(cr.get("returned_opened"))
        b["converted"] += bool(cr.get("converted"))
        b["conv_opened"] += bool(cr.get("converted_opened"))
        b["converted90"] += bool(cr.get("converted90"))
        b["converted30"] += bool(cr.get("converted30"))
    _add_mature(b, rec, today)

# Data tab layout: row 1 = do-not-edit note, row 2 = header, rows 3+ = data.
# Cols: A Gym, B Name, then one column per METRIC_HEADERS entry, then
# Level, Section, Tag, Updated, GymOrder (sort key so "All gyms" rows sit
# above gym rows).
DATA_HEADER = (["Gym", "Automation / email version"] + METRIC_HEADERS
               + ["Level", "Section", "Tag", "Updated", "GymOrder"]
               + [h for h, _w in MATURE_KEYS.values()])
D0, D1 = 3, 500  # data row span referenced by every formula
COMBINED = "All gyms total"  # gym label of the cross-gym per-automation rows
GYM_ORDER = {COMBINED: 0, "SHIFT": 1, "ABC": 2}

# --- column indexes, all DERIVED from METRIC_KEYS ---------------------------
# Adding "Clicks per open %" (Chris's Variants!H5 comment, 2026-08-06) meant
# every hardcoded offset in the writer shifted by one. Rather than hand-edit
# ten separate magic-number lists in two repos and hope, the writer now asks
# METRIC_KEYS where each column is. Insert a metric anywhere in the two lists
# above and the Data tab, the FILTER formulas, the percent formatting, the
# combined-row math and the History tab all move with it.
_METRIC_C0 = 2  # a Data row starts [Gym, Name] before the metric columns
# the maturity counts start after Level, Section, Tag, Updated, GymOrder
_MAT_C0 = _METRIC_C0 + len(METRIC_KEYS) + 5


def _mi(key: str) -> int:
    """0-based index of a metric column (or a maturity count) in a Data row."""
    if key in MATURE_KEYS:
        return _MAT_C0 + _MAT_KEYS.index(key)
    return _METRIC_C0 + METRIC_KEYS.index(key)


def _a1col(c: int) -> str:
    """0-based column index -> A1 letter ("A", "B", ... "AA")."""
    s = ""
    c += 1
    while c:
        c, r = divmod(c - 1, 26)
        s = chr(65 + r) + s
    return s


def _col(key: str) -> str:
    """A1 column letter of a metric column on the Data tab."""
    return _a1col(_mi(key))


# count metrics (summable); the rest are ratios recomputed from these
_COUNT_KEYS = ("sends", "delivered", "opens", "clicks", "unsubs", "taps",
               "responses", "redeems", "purchases", "returned", "ret_opened",
               "converted", "conv_opened", "converted90", "converted30")
# everything a total adds up: the visible counts plus the maturity counts
_SUM_KEYS = _COUNT_KEYS + _MAT_KEYS
# ratio metric -> (numerator key, denominator key). Open % and Unsubscribe %
# swap their denominator to sends when a gym has no delivery tracking (see
# _DELIVERED_DEN_KEYS and _totals_formulas). The outcome rates read the
# maturity counts (Block 15, 2026-09-24): only emails whose window has
# finished are in the top or the bottom of the fraction.
_RATIO_KEYS = {
    "open_pct": ("opens", "delivered"),
    "cpo_pct": ("clicks", "opens"),
    # Tasks step 1.3 (2026-09-18): of the emails that arrived, how many made
    # the person unsubscribe (the unsubscribe pinned to THIS email)
    "unsub_pct": ("unsubs", "delivered"),
    "resp_pct": ("m_resp14", "m_sends14"),
    "return_pct": ("m_ret30", "m_sends30"),
    "conv_pct": ("m_conv60", "m_sends60"),
    # Chris's ask 2026-09-15: of the people who READ the email, how many came
    # back / joined with it as their last touch ("opened first" over Opens)
    "ret_opened_pct": ("m_retop30", "m_opens30"),
    "conv_opened_pct": ("m_convop60", "m_opens60"),
    "conv90_pct": ("m_conv90", "m_sends90"),
    "conv30_pct": ("m_conv30", "m_sends30"),
}
# ratios whose denominator is Delivered when it is known, else Sends (every
# rule that recomputes a ratio must treat these the same way)
_DELIVERED_DEN_KEYS = ("open_pct", "unsub_pct")
_COUNT_IDX = [_mi(k) for k in _SUM_KEYS]
_PCT_IDX = [_mi(k) for k in _RATIO_KEYS]
I_SENDS, I_DELIV, I_OPENS = _mi("sends"), _mi("delivered"), _mi("opens")
I_NOTE = _mi("note")
I_LEVEL, I_SECTION, I_TAG = I_NOTE + 1, I_NOTE + 2, I_NOTE + 3
I_UPDATED, I_ORDER = I_NOTE + 4, I_NOTE + 5
DATA_NCOLS = _MAT_C0 + len(_MAT_KEYS)
DATA_LASTCOL = _a1col(DATA_NCOLS - 1)
# plain-words note on each rate's header cell (Dashboard + Variants): which
# emails sit in the bottom of the fraction (Block 15 maturity, 2026-09-24)
PCT_NOTES = {
    "open_pct": "Opens / Delivered (Sends where Delivered is blank). Every email "
                "counts; opens are counted for 14 days after the send.",
    "cpo_pct": "Link clicks / Opens. Every email counts.",
    "unsub_pct": "Unsubscribes / Delivered (Sends where Delivered is blank). "
                 "Every email counts.",
    "resp_pct": "Only emails sent 14 or more days ago (the survey's answer "
                "window has closed): answered / those emails. Newer emails are "
                "in Responses but not in this %.",
    "return_pct": "Only emails sent 30 or more days ago (the came-back window "
                  "has closed): came back / those emails. Newer emails are in "
                  "Returned after send but not in this %.",
    "ret_opened_pct": "Only emails sent 30 or more days ago: came back after "
                      "opening / the opens of those emails.",
    "conv_pct": "Only emails sent 60 or more days ago (the 60-day join window "
                "has closed): joined / those emails. Newer emails are in Joined "
                "within 60 days but not in this %.",
    "conv_opened_pct": "Only emails sent 60 or more days ago: joined after "
                       "opening / the opens of those emails.",
    "conv30_pct": "Only emails sent 30 or more days ago (the 30-day join window "
                  "has closed): joined / those emails. Newer emails are in Joined "
                  "within 30 days but not in this %.",
    "conv90_pct": "Only emails sent 90 or more days ago (the 90-day join window "
                  "has closed): joined / those emails. Blank until the first "
                  "email is 90 days old.",
}


def _outcome_rates(b: dict) -> dict:
    """The ratio columns of one bucket of summed counts (mature keys included),
    blank when the bottom is 0. Open % and Unsubscribe % are NOT here: each
    gym computes those itself (ABC falls back to sends)."""
    out = {}
    for key, (num, den) in _RATIO_KEYS.items():
        if key not in _DELIVERED_DEN_KEYS:
            out[key] = _pct(b.get(num, 0), b.get(den, 0))
    return out

FOOTNOTES = [
    "Why these numbers can differ from Mailchimp's screens: Mailchimp still "
    "counts old test sends that can't be removed, and it counts an 'open' even "
    "when the open belonged to a different email. This sheet only counts real "
    "climbers and pins every open to the exact email we sent. Trust this sheet "
    "and the Send It dashboards.",
    "Why they can differ from Beta/RGP screens too: those systems count all "
    "visitors and sales all day. This sheet only looks at people we emailed, "
    "and only at what they did after the email.",
    "Opens include Apple Mail's automatic opens, at both gyms: Apple loads "
    "every email for its users whether or not they read it. So an open is "
    "not proof of reading. It lifts open rates equally for every version, so "
    "comparisons stay fair.",
    "Offer redemptions: SHIFT counts a day pass bought at 50% or more off "
    "after the offer email (Beta never records which coupon was used). ABC "
    "counts the 'email discount' product (or an under-$8.25 day pass) on RGP "
    "invoices.",
    "Delivered: SHIFT counts Mailchimp's send confirmation, ABC counts "
    "Brevo's delivered receipt. A blank Delivered means that system had no "
    "delivery info for those emails, and that row's Open % is out of sends. "
    "Brevo keeps 90 days of receipts, so an ABC send with no receipt inside "
    "that window stays uncounted.",
    "Member survey (ABC): the only row on this sheet that goes to current "
    "members, not first-timers. Returned, converted, redeemed and purchases "
    "are blank there because they describe first-timers.",
    "'Before the test started' rows on Variants: SHIFT's survey and offer "
    "emails went live before their subject tests did, so their earliest sends "
    "have no A or B version. Those sends sit in their own row so each test "
    "block still adds up to the Dashboard total for the same email. They are "
    "greyed out and sit at the bottom of their block because they are context, "
    "not an arm. Leave them out when comparing A against B.",
    "Clicks per open %: of the people who opened the email, how many clicked "
    "a link in it. Open % tells you whether the subject line worked; this "
    "column tells you whether the email itself worked once it was opened. It "
    "is blank when nobody opened yet, because there is nothing to divide by.",
    "Greyed rows inside a test block are context, not versions being tested. "
    "They sit at the bottom of their block, and they are the people the test "
    "could not include. Compare only the rows above them.",
    "How Returned, Joined, Offer redemptions and Purchases are counted "
    "(since 2026-09-14): each person counts once. The credit goes to the LAST "
    "email they got before they acted, and only if they acted within that "
    "email's window (came back: 30 days, joined: 60 or 90, redeemed: 30, "
    "bought: 30, answered the survey: 14, survey emails only). Before this "
    "date every email a person got took credit for anything they did later. "
    "Since 2026-09-18 the History tab is recounted this way on every run, so "
    "every month back to July 2026 uses the same counting. 'Opened first' = "
    "how many of those people had opened that email before they acted (a few "
    "opens are automatic, see above).",
    "Joined (since 2026-09-24): the first paid membership that is not a youth "
    "plan, started within 60 days of the email (and, in its own columns, "
    "within 90 days). Youth memberships never count as a join anywhere: the "
    "child joins, not the person we emailed. Until 2026-09-24 these columns "
    "were called 'Converted' and had no 90-day version.",
    "Which emails are in each % (since 2026-09-24): a rate only counts the "
    "emails whose window has closed, so a send from last week is never read "
    "as a miss. Response % uses emails at least 14 days old, Return % 30 days, "
    "Join % (60 days) 60 days, Join % (90 days) 90 days. The counts next to "
    "each rate include every email. Hover a % header for its exact rule. The "
    "Pilot scorecard tab and the Experiments tab work the same way.",
    "Link clicks (since 2026-09-24): a click on a link in THIS email, "
    "including the rating buttons and every survey link that goes through "
    "the gym's Send It link. Until then a click was counted for any email "
    "sent before it and the rating buttons were not counted as clicks; "
    "older emails are re-measured once under the new rule (ABC: only the "
    "last 90 days, which is all Brevo keeps).",
    "'% of readers' (since 2026-09-15): of the people who OPENED this email, "
    "how many came back or joined with it as their last email ('opened "
    "first' divided by Opens). Blank when nobody has opened yet. Automatic "
    "opens (see above) sit in the bottom of this fraction, so it reads a "
    "little low, equally for every version.",
    "Unsubscribes (since 2026-09-18): people who clicked the unsubscribe link "
    "in THIS email. Each unsubscribe is pinned to the exact email whose link "
    "was used, the same way opens are, so a person who leaves counts once, "
    "on the email that made them leave. Unsubscribe % divides by Delivered "
    "(or by Sends where Delivered is blank). Anyone who unsubscribes is "
    "already left out of every later send. Older sends are being measured a "
    "batch a day at ABC (Brevo keeps 90 days of events), so ABC's older "
    "counts fill in over the first week.",
]

# Plain-English hover glossary (Luke 2026-07-28): shown as a cell note on each
# section's Automation / Email version header, because notes glued to data
# cells would describe the wrong row once the gym filter shifts the rows.
DESCRIPTIONS = {
    "FTV survey (day 1-2)":
        "Feedback survey email. Goes to every first-timer 1 to 2 days after "
        "their first visit.",
    "Reengage 50% offer (day 5-7)":
        "Come-back offer carrying the 50% off code. Goes 5 to 7 days after "
        "the first visit to people who have not returned and did not answer "
        "the survey.",
    "Round two (high intent)":
        "Follow-up offer for survey responders who said they are likely to "
        "come back.",
    "Membership Lite offer (0-14d after last day pass)":
        "Membership Lite offer for day-pass regulars: people who bought 2 or "
        "more day passes within 30 days, are not members and never bought a "
        "trial. Sent 0 to 14 days after their last day pass, once per person.",
    "Offer reminder (day 9-10)":
        "Reminder that the come-back offer is still good. Goes 9 to 10 days "
        "after the first visit to people who got the offer and have not "
        "returned.",
    "Membership offer (2-3d after 2nd visit)":
        "Membership email sent 2 to 3 days after someone comes back for a "
        "second visit within 30 days of their first.",
    "Member survey (every 180 days)":
        "ABC's short satisfaction survey for CURRENT members, 10 a day, each "
        "member at most once every 180 days. Not part of the first-timer "
        "funnel.",
    "Blocker: pricing":
        "Offer email tailored to survey responders whose main issue was price.",
    "Blocker: crowding":
        "Offer email tailored to survey responders whose main issue was "
        "crowding.",
    "Blocker: too hard":
        "Offer email tailored to survey responders who found climbing too "
        "hard.",
    "Blocker: intimidating":
        "Offer email tailored to survey responders who felt intimidated.",
    "Blocker: front desk":
        "Offer email tailored to survey responders who had a front desk "
        "problem.",
    "Blocker: confusing":
        "Offer email tailored to survey responders who found the visit "
        "confusing.",
    "Blocker: routes":
        "Offer email tailored to survey responders who did not enjoy the "
        "routes.",
}
VARIANT_DESCRIPTIONS = {
    "Subject A + link to the survey":
        "Survey email, subject line A, answered through a link to the form.",
    "Subject B + link to the survey":
        "Survey email, subject line B, answered through a link to the form.",
    "Subject A + rating buttons in the email":
        "Survey email, subject line A, question 1 answered by tapping a "
        "rating button inside the email.",
    "Subject B + rating buttons in the email":
        "Survey email, subject line B, question 1 answered by tapping a "
        "rating button inside the email.",
    "Offer subject A": "The come-back offer email with subject line A.",
    "Offer subject B": "The come-back offer email with subject line B.",
    PRETEST_LABEL:
        "Sends made before this A/B test existed, when the email had only one "
        "version. They are counted here so this block adds up to the "
        "Dashboard total for the same email, but they are not part of the A "
        "vs B comparison: both arms started on the same day, after these.",
    "Subject A": "Arm A of this email's subject line test.",
    "Subject B": "Arm B of this email's subject line test.",
    "Membership offer (A)":
        "The membership email (Lite $38 / Full $69), sent 2 to 3 days after a "
        "second visit. This is the control half of the membership vs trial "
        "test.",
    "2-week trial offer (B)":
        "The $29 two-week trial email, sent to the other half of the same "
        "group at the same point in the flow. Same people, same timing, "
        "different offer, so a difference here is the offer.",
    OUT_OF_TEST_LABEL:
        "Returners who could not be put in the test: they already bought a "
        "two-week trial once (SHIFT sells one per person, ever) or their zip "
        "code is outside West Michigan or unknown. They still get the "
        "membership email, exactly as before. Counted here so the block adds "
        "up, but leave them out when comparing A against B.",
}
DASH_HOWTO = (
    "How to read this table: pink rows are SHIFT, orange rows are ABC. Bold "
    "gray rows labeled 'All gyms total' add the gyms together and only appear "
    "when the Show dropdown is on All gyms. A 'no sends yet' row is a live "
    "automation that has not sent its first email. Hover each section's "
    "Automation header for what each email is.")

GYM_FILLS = {  # gym -> (base fill, alternating fill)
    "SHIFT": ("#f9e3ee", "#f3d3e6"),
    "ABC": ("#fdeadd", "#fbdfc8"),
}
BAND_BG, BAND_FG = "#b0bec5", "#000000"  # all text black (Luke, 2026-07-28)
HEADER_BG = "#eceff1"
NOTE_FG = "#000000"
PRETEST_FG = "#7f8c8d"  # grey text on the context-only pre-test rows


# --------------------------------------------------------------------------
# gather (SHIFT-specific; the ABC port swaps this half for Brevo/RGP)
# --------------------------------------------------------------------------

def _sheets_service():
    # One Resource tree for the whole push. Rebuilding it per call (the old
    # build() here + a fresh spreadsheets().values() on every request) cost
    # ~20 MB per Sheets call that never came back; see drive_io.sheets_service.
    return drive_io.sheets_service("https://www.googleapis.com/auth/spreadsheets")


def _pull_outreach(dest: Path) -> list[dict]:
    try:
        drive_io.pull(OUTREACH_DRIVE_ID, str(dest))
    except Exception as exc:  # stale copy beats no stats
        print(f"  allgyms: outreach pull failed ({exc}); using existing copy")
    if not dest.exists():
        return []
    with open(dest, encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f)
                if (r.get("mode") or "").strip() != "test"]


def _cache_path() -> Path:
    return BASE / "_allgyms_engagement_cache.csv"


def _load_engagement_cache() -> dict[tuple, tuple]:
    """(email, sent_date, tag) -> (delivered, opened, clicked, opened_at,
    unsubscribed, click_rule). A cache file written before 2026-09-18 has no
    unsubscribed column and reads as None (not measured yet, one backfill
    fetch due); one written before 2026-09-24 has no click_rule and reads ""
    (clicked under the older rule, one re-measure due).

    Fails soft to {} at every step: an unreadable cache costs a slower run that
    re-measures everything, never a wrong number."""
    if not ENGAGEMENT_CACHE_DRIVE_ID:
        return {}
    try:
        drive_io.pull(ENGAGEMENT_CACHE_DRIVE_ID, str(_cache_path()))
    except Exception as exc:
        print(f"  allgyms: engagement cache pull failed ({exc}); "
              f"re-measuring every send this run")
    if not _cache_path().exists():
        return {}
    out: dict[tuple, tuple] = {}
    try:
        with open(_cache_path(), encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                key = ((r.get("email") or "").strip().lower(),
                       (r.get("sent_date") or "").strip()[:10],
                       (r.get("tag") or "").strip())
                if not all(key):
                    continue
                out[key] = (r.get("delivered") == "1", r.get("opened") == "1",
                            r.get("clicked") == "1",
                            (r.get("opened_at") or "").strip()[:10],
                            _flag_or_blank(r.get("unsubscribed")),
                            (r.get("click_rule") or "").strip())
    except (OSError, csv.Error) as exc:
        print(f"  allgyms: engagement cache unreadable ({exc}); "
              f"re-measuring every send this run")
        return {}
    return out


def _save_engagement_cache(measured: dict[tuple, tuple], stamp: str) -> None:
    """Write back one row per send in the CURRENT outreach log, so the cache
    stays exactly as long as the log and never accumulates orphans. A failure
    here is harmless: next run just re-measures."""
    if not ENGAGEMENT_CACHE_DRIVE_ID:
        return
    try:
        with open(_cache_path(), "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CACHE_FIELDS)
            w.writeheader()
            for (email, sent, tag), (d, o, c, oa, un, cr) in sorted(measured.items()):
                w.writerow({"email": email, "sent_date": sent, "tag": tag,
                            "delivered": int(d), "opened": int(o),
                            "clicked": int(c), "opened_at": oa or "",
                            "open_kind": "",
                            "unsubscribed": _cell_or_blank(un),
                            "click_rule": cr or "",
                            "frozen_at": stamp})
        drive_io.push(str(_cache_path()), file_id=ENGAGEMENT_CACHE_DRIVE_ID)
    except Exception as exc:
        print(f"  allgyms: engagement cache push failed ({exc}); "
              f"next run will re-measure")


def _tx_dates(client) -> tuple[dict, dict]:
    """All SUCCEEDED transaction dates, keyed by climber_id and by email."""
    by_cid: dict[str, list] = defaultdict(list)
    by_email: dict[str, list] = defaultdict(list)
    with open(client.transactions_csv, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("state") != "SUCCEEDED":
                continue
            d = (row.get("time") or "")[:10]
            if not d:
                continue
            cid = (row.get("climber_id") or "").strip()
            em = (row.get("climber_email") or "").strip().lower()
            if cid:
                by_cid[cid].append(d)
            if em:
                by_email[em].append(d)
    return by_cid, by_email


def _pct(a: int, b: int):
    return round(a / b, 4) if b else ""


# Per-send facts from the last collect(): email, sent, tag, var_tag, the
# measured flags and the attribution credits. Read by the Experiments v2
# writer (block 6, 2026-09-14) so it can count PEOPLE per arm.
SEND_RECORDS: list[dict] = []
# The Pilot scorecard from the last collect() (engine.build_scorecard, the same
# call the site's Insights slide makes), for the Scorecard tab (Block 15).
SCORECARD: dict = {}
# The maturity cut-off date of the last collect() (the data's last check-in
# day), read by the History recount so both tabs cut at the same day.
MATURE_ASOF: list = [None]


def collect(client) -> tuple[list[dict], list[dict]]:
    """This gym's stats -> (automation rows, variant rows), one dict each:
    {gym, name, section, tag, level, m: {metric_key: value}}"""
    rows = _pull_outreach(BASE / "_allgyms_outreach_snapshot.csv")
    stage(f"outreach snapshot pulled ({len(rows)} rows)")
    ds = ingest.load(client)
    stage(f"ingest.load done ({len(ds.climbers)} climbers)")
    # maturity cut-off (Block 15) = the last check-in in the data, the
    # scorecard's own clock, so a stale data pull never reads a young email
    # as a miss; never later than today
    _smax = getattr(ds, "sessions_max_date", None)
    mature_asof = datetime.now(timezone.utc).date()
    if _smax:
        mature_asof = min(mature_asof, date.fromisoformat(str(_smax)[:10]))
    MATURE_ASOF[0] = mature_asof
    mc = MailchimpClient(config.load_settings(client, require=True))
    tx_by_cid, tx_by_email = _tx_dates(client)
    stage("transaction dates indexed")

    sends: list[dict] = []
    for r in rows:
        email = (r.get("email") or "").strip().lower()
        sent = (r.get("sent_date") or "").strip()[:10]
        trig = (r.get("trigger_name") or r.get("trigger") or "").strip()
        tag = (r.get("tag") or "").strip()
        if email and sent and tag:
            sends.append({"email": email, "sent": sent, "trig": trig, "tag": tag})

    # Mailchimp activity feeds. Only for people with a send that still needs
    # measuring: inside the fresh window, or older but not in the cache yet
    # (first run after the cache was switched on, and any backfill after that).
    # Everyone else's numbers come from the frozen cache, so this loop stops
    # growing with the outreach log. See ENGAGEMENT_CACHE_DRIVE_ID above.
    cache = _load_engagement_cache()
    fresh_from = (datetime.now(timezone.utc).date()
                  - timedelta(days=ENGAGEMENT_FRESH_DAYS)).isoformat()

    def _is_fresh(s: dict) -> bool:
        """True = measure live this run. Sends inside the window always are;
        older ones when the cache has no answer for them yet, holds an open
        with no date yet (one backfill fetch, 2026-09-14; the merge below
        writes "unknown" if the event is gone, so never twice), or has no
        unsubscribe measurement yet (one backfill fetch, 2026-09-18; the
        merge always writes 0 or 1, so never twice), or a click measured
        under the older rule (one re-measure, 2026-09-24; the merge always
        writes the current click_rule, so never twice)."""
        if s["sent"] >= fresh_from:
            return True
        hit = cache.get((s["email"], s["sent"], s["tag"]))
        return (hit is None or bool(hit[1] and not hit[3]) or hit[4] is None
                or hit[5] not in definitions.CLICK_SETTLED)

    need = [s for s in sends if _is_fresh(s)]
    emails = sorted({s["email"] for s in need})
    _blank = (False, False, False, "", None, "")
    unsub_pending = sum(1 for s in need if s["sent"] < fresh_from
                        and (cache.get((s["email"], s["sent"], s["tag"]))
                             or _blank)[4] is None)
    click_pending = sum(1 for s in need if s["sent"] < fresh_from
                        and (cache.get((s["email"], s["sent"], s["tag"]))
                             or _blank)[5] not in definitions.CLICK_SETTLED)
    stage(f"activity feeds: {len(emails)} to fetch "
          + (f"(unsubscribe backfill: {unsub_pending} older send(s) still to measure) "
             if unsub_pending else "")
          + (f"(click recount: {click_pending} older send(s) to re-measure on this email) "
             if click_pending else "")
          + f"({len(need)} of {len(sends)} sends live, "
          f"{len(sends) - len(need)} from cache, "
          f"window={ENGAGEMENT_FRESH_DAYS}d from {fresh_from}, "
          f"cache={'on' if ENGAGEMENT_CACHE_DRIVE_ID else 'OFF'})")
    def _feed(e: str) -> list:
        """One activity feed, retried twice on a transient failure (a
        timeout, a dropped connection, a Mailchimp 429 / 5xx). The one-time
        click recount (2026-09-24) reads ~700 feeds in one run, so a single
        blip must not cost the whole push; a failure that survives the
        retries still raises, so no zeros are ever frozen into the cache."""
        for attempt in range(3):
            try:
                return mc.member_activity(e)
            except Exception as exc:  # noqa: BLE001
                status = getattr(exc, "status", None)
                transient = status is None or status == 429 or status >= 500
                if attempt == 2 or not transient:
                    raise
                print(f"  allgyms: Mailchimp feed for one contact failed ({exc}); "
                      f"retry {attempt + 1} of 2")
                time.sleep(2 * (attempt + 1))
        return []

    feeds = {}
    for i, e in enumerate(emails, 1):
        feeds[e] = _feed(e)
        if i % 25 == 0:
            stage(f"activity feeds {i}/{len(emails)}")
    stage(f"activity feeds done ({len(emails)} emails, "
          f"{sum(len(v or []) for v in feeds.values())} events)")
    cids = {a.get("campaign_id") for ev in feeds.values()
            for a in ev or [] if a.get("campaign_id")}
    journey_ids = {c for c in cids if mc.campaign_type(c) == "automation-email"}
    stage(f"campaign types resolved ({len(cids)} campaigns, "
          f"{len(journey_ids)} journey emails)")

    climber_by_email: dict[str, object] = {}
    for x in ds.climbers.values():
        e = (x.email or "").strip().lower()
        if e and e not in climber_by_email:
            climber_by_email[e] = x

    # survey responses by email (earliest)
    resp_by_email: dict[str, str] = {}
    try:
        for r in survey.load_responses(client):
            e = (r.email or "").strip().lower()
            d = (r.answered_at or "")[:10]
            if e and (e not in resp_by_email or d < resp_by_email[e]):
                resp_by_email[e] = d
    except Exception as exc:
        print(f"  allgyms: survey read failed ({exc}); responses omitted")
    stage(f"survey responses read ({len(resp_by_email)})")

    # Q1 taps (embed arms), first valid tap per email only
    taps_valid: dict[str, str] = {}
    try:
        svc = _sheets_service()
        got = svc.spreadsheets().values().get(
            spreadsheetId=client.survey.get("gsheet_id"),
            range="Taps!A:D").execute(num_retries=_NUM_RETRIES).get("values", [])
        embed_sent = {s["email"]: s["sent"] for s in sends if s["tag"] in EMBED_TAGS}
        for r in got[1:]:
            if len(r) < 4:
                continue
            ts, email, q = r[0][:10], r[1].strip().lower(), r[2]
            if email in embed_sent and ts >= embed_sent[email] \
                    and email not in taps_valid:
                taps_valid[email] = q
    except Exception as exc:
        print(f"  allgyms: taps read failed ({exc}); taps omitted")
    stage(f"Q1 taps read ({len(taps_valid)} valid)")

    click_markers = definitions.SURVEY_LINK_MARKERS + definitions.BUY_LINK_MARKERS
    by_trig: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    by_tag: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    trig_of_tag: dict[str, str] = {}
    measured: dict[tuple, tuple] = {}  # what this run knows -> the next cache
    records: list[dict] = []  # per-send facts for the Experiments v2 writer
    for s in sends:
        email, sent = s["email"], s["sent"]
        key = (email, sent, s["tag"])
        prev = cache.get(key)
        hit = prev if not _is_fresh(s) else None
        if hit is not None:
            # settled send: keep the delivered/opened/clicked already measured
            delivered, opened, clicked, opened_at, unsubscribed, click_rule = hit
        else:
            ev = feeds.get(email) or []
            sent_ids = engine._sent_campaign_ids(ev, sent, journey_ids)
            delivered = bool(sent_ids)
            opened = delivered and engine._has_event(
                ev, "open", sent, campaign_ids=sent_ids)
            # a click on a link in THIS email (campaign id, like an open),
            # survey form, /s survey links and buy links (Block 15, 2026-09-24)
            clicked = delivered and engine._has_event(
                ev, "click", sent, click_markers, campaign_ids=sent_ids)
            click_rule = definitions.CLICK_RULE
            opened_at = engine._first_event_date(
                ev, "open", sent, campaign_ids=sent_ids) if opened else ""
            # the unsubscribe link used in THIS email: Mailchimp's `unsub`
            # activity carries the campaign id, so it pins like an open
            # (Tasks step 1.3, 2026-09-18)
            unsubscribed = delivered and engine._has_event(
                ev, "unsub", sent, campaign_ids=sent_ids)
            if prev is not None:
                # never downgrade a frozen flag: a feed that no longer shows an
                # event (Mailchimp trims old activity, a partial answer) must not
                # erase a measured open; and a backfill fetch that finds no date
                # writes "unknown" so the row is not asked again
                # the older click rule over-counted, so its flag is replaced
                # once, not kept (see click_rule at CACHE_FIELDS). But the
                # feed holds only a person's newest 50 events: when it no
                # longer shows THIS email's send, the click cannot be pinned,
                # so the older flag is kept and marked CLICK_RULE_KEPT (never
                # asked again) rather than wiped (fresh-eyes finding).
                if prev[5] == definitions.CLICK_RULE:
                    clicked = clicked or prev[2]
                elif not sent_ids:
                    clicked, click_rule = prev[2], definitions.CLICK_RULE_KEPT
                delivered, opened = delivered or prev[0], opened or prev[1]
                unsubscribed = unsubscribed or bool(prev[4])
                if opened and not opened_at:
                    opened_at = prev[3] or OPENED_AT_UNKNOWN
        measured[key] = (delivered, opened, clicked, opened_at, unsubscribed,
                         click_rule)
    _uns = [v[4] for v in measured.values()]
    stage(f"unsubscribes: {sum(1 for u in _uns if u)} pinned to a send, "
          f"{sum(1 for u in _uns if u is None)} send(s) not measured yet")

    # Outcomes (roadmap block 6 phase B, 2026-09-14): one credit per person
    # per outcome, to the LAST send before the event, inside that send's
    # window; and whether that email had been opened first. Before this every
    # send took credit for anything later (3 emails + 1 join = 3 credits).
    # Survey responses credit survey emails only. Same rule in the ABC copy.
    events: dict[str, dict] = {}
    for s in sends:
        email = s["email"]
        if email in events:
            continue
        c = climber_by_email.get(email)
        txs = (tx_by_cid.get(c.climber_id, []) if c else []) + tx_by_email.get(email, [])
        events[email] = {
            "returned": list(c.visit_days) if c else [],
            # the REPORTING join date (Block 15, 2026-09-24): youth plans
            # never count as a join (definitions.first_join)
            "converted": ([c.join_date] if c
                          and getattr(c, "join_date", "") else []),
            "redeemed": list(c.discounted_daypass_dates) if c else [],
            "purchased": list(txs),
            "responded": [resp_by_email[email]] if email in resp_by_email else [],
        }

    def _send_obj(s: dict) -> attribution.Send:
        _d, o, _c, oa, _u, _r = measured[(s["email"], s["sent"], s["tag"])]
        return attribution.Send(email=s["email"], sent=s["sent"], tag=s["tag"],
                                trig=s["trig"], opened=o, opened_at=oa)
    credits = attribution.credit([_send_obj(s) for s in sends], events)
    # "Joined within 90 days" (Block 15): the same last-touch credit with the
    # join window stretched to 90 days
    credits90 = attribution.credit(
        [_send_obj(s) for s in sends], events,
        windows={"converted": definitions.JOIN_DAYS_LONG})
    credits30 = attribution.credit(
        [_send_obj(s) for s in sends], events,
        windows={"converted": definitions.JOIN_DAYS_SHORT})
    resp_credits = attribution.credit(
        [_send_obj(s) for s in sends if "survey" in s["tag"]], events)
    stage("outcomes credited (one per person, last touch, windowed)")

    for s in sends:
        email, sent = s["email"], s["sent"]
        key = (email, sent, s["tag"])
        delivered, opened, clicked, _oa, _unsub, _rule = measured[key]
        unsubscribed = bool(_unsub)  # None (not measured yet) counts as 0
        cr = credits[key]
        responded = (resp_credits.get(key) or {}).get("responded", False)
        # the record's credits carry the SURVEY-ONLY response credit (an
        # answer is credited to the last SURVEY email before it, never to the
        # 50% offer that followed), so a bucket test reads the same rule as
        # the Responses column (fresh-eyes finding, 2026-09-23)
        cr = dict(cr)
        cr["responded"] = bool(responded)
        cr["responded_opened"] = bool((resp_credits.get(key) or {}).get("responded_opened"))
        cr["converted90"] = bool(credits90[key]["converted"])
        cr["converted90_opened"] = bool(credits90[key]["converted_opened"])
        cr["converted30"] = bool(credits30[key]["converted"])
        tapped = s["tag"] in EMBED_TAGS and email in taps_valid
        # the join date rides along so a yardstick tighter than the 60-day
        # credit window (E-005 at 30 days, step 1.2, 2026-09-18) can check it
        _ev_c = (events.get(email) or {}).get("converted") or []
        rec = {"email": email, "sent": sent, "tag": s["tag"],
               "trig": s["trig"] or s["tag"], "var_tag": s["tag"],
               "delivered": delivered, "opened": opened, "clicked": clicked,
               "opened_at": _oa, "credits": cr, "responded": responded,
               # the answer date rides along so a bucket test can
               # judge "answered within 14 days of the FIRST email"
               # (survey reminder test, Tasks 2.2, 2026-09-23)
               "responded_at": resp_by_email.get(email, ""),
               "converted_at": _ev_c[0] if _ev_c else "",
               "unsubscribed": unsubscribed,
               # History recount (2026-09-18): every SHIFT send is
               # first-timer outreach (no member survey here yet)
               "tapped": tapped, "ftv": True}
        records.append(rec)
        for bucket, key in ((by_trig, s["trig"] or s["tag"]), (by_tag, s["tag"])):
            _bucket_add(bucket[key], rec, mature_asof)
        trig_of_tag[s["tag"]] = s["trig"] or s["tag"]

    # zero-rows (Luke 2026-07-28): every ACTIVE automation and every test arm
    # shows on the sheet even before its first send, with 0 values. Inactive
    # triggers (trial win-back, day pass to trial) stay off the sheet.
    for t in client.triggers:
        if t.active:
            by_trig[t.name]
    for tag in TEST_ARMS:
        if tag in SEED_WHEN_SENT and tag not in by_tag:
            continue  # no row on Variants until the first send exists
        by_tag[tag]

    def metrics(b: dict) -> dict:
        m = {k: b[k] for k in _SUM_KEYS}
        zero = b["sends"] == 0
        m["open_pct"] = 0 if zero else _pct(b["opens"], b["delivered"])
        # Tasks step 1.3 (2026-09-18): unsubscribes pinned to this email, over
        # delivered (Mailchimp always reports it; the ABC copy falls back to
        # sends when Brevo has no receipt)
        m["unsub_pct"] = 0 if zero else _pct(b["unsubs"], b["delivered"])
        # every other rate from the shared rule (clicks per open; the outcome
        # rates over MATURE emails only, Block 15). Blank, never 0, when the
        # bottom is 0, so an unopened or too-new row cannot read as 0%.
        for k, v in _outcome_rates(b).items():
            m[k] = 0 if zero else v
        m["note"] = ("no sends yet" if zero else
                     f"small sample (under {SMALL_N}), directional only"
                     if b["sends"] < SMALL_N else "")
        return m

    auto_rows = [{
        "gym": GYM, "name": FUNNEL_LABELS.get(k, k), "section": _section_of(k),
        "tag": "", "level": "automation", "m": metrics(by_trig[k]),
    } for k in by_trig]
    var_rows = [{
        "gym": GYM, "name": TEST_ARMS[k][1], "section": TEST_ARMS[k][0],
        "tag": k, "level": "variant", "m": metrics(by_tag[k]),
    } for k in by_tag if k in TEST_ARMS]
    # Safe to freeze because a Mailchimp failure never reaches this line: an
    # unknown contact returns [] (a real answer) and any other API error raises
    # out of the feed loop, failing the whole stats push before anything is
    # written. Do NOT wrap that loop in a try/except without also gating this
    # call, or one bad Mailchimp day would freeze zeros forever (see ABC's
    # feeds_ok flag, where the ESP failure IS swallowed).
    _save_engagement_cache(measured, _stamp())
    stage(f"collect done ({len(auto_rows)} automation rows, "
          f"{len(var_rows)} variant rows, {len(measured)} sends cached)")
    SEND_RECORDS[:] = records
    # the Scorecard tab's numbers; guarded so a scorecard bug can never cost
    # the Data / Dashboard / History push, and loud (rule 5) when it breaks
    SCORECARD.clear()
    try:
        SCORECARD.update(engine.build_scorecard(ds, client))
    except Exception:
        print("  allgyms: Scorecard build FAILED (tab left as it was): "
              + traceback.format_exc())
    return auto_rows, var_rows


# --------------------------------------------------------------------------
# sheet writer (gym-agnostic)
# --------------------------------------------------------------------------

def _stamp() -> str:
    now = datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return now.astimezone(ZoneInfo("America/New_York")) \
                  .strftime("%Y-%m-%d %I:%M %p ET")
    except Exception:
        return now.strftime("%Y-%m-%d %H:%M UTC")


def _hex(h: str) -> dict:
    h = h.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255,
            "blue": int(h[4:6], 16) / 255}


def _data_row(r: dict, stamp: str) -> list:
    return ([r["gym"], r["name"]] + [r["m"][k] for k in METRIC_KEYS]
            + [r["level"], r["section"], r["tag"], stamp,
               GYM_ORDER.get(r["gym"], 9)]
            + [r["m"].get(k) or 0 for k in _MAT_KEYS])


def _normalize_row(row: list) -> list:
    """Rows read back from the sheet arrive as strings; make the metric cells
    numbers again so combined-row math and sheet formulas keep working."""
    row = row + [""] * (DATA_NCOLS - len(row))
    for i in _COUNT_IDX:
        if i == I_DELIV and row[i] in ("", None):
            row[i] = ""  # delivered is unknown for Brevo gyms, keep it blank
            continue
        try:
            row[i] = int(float(row[i]))
        except (TypeError, ValueError):
            row[i] = "" if i == I_DELIV else 0
    for i in _PCT_IDX:
        try:
            row[i] = float(row[i])
        except (TypeError, ValueError):
            row[i] = ""
    row[I_ORDER] = GYM_ORDER.get(row[0], 9)
    return row


def _combined_rows(per_gym: list[list], stamp: str) -> list[list]:
    """One 'All gyms' total row per automation that 2+ gyms share. With a
    single gym in the sheet these would duplicate its rows, so none appear."""
    groups: dict[tuple, list] = defaultdict(list)
    for row in per_gym:
        if row[I_LEVEL] == "automation":
            groups[(row[I_SECTION], row[1])].append(row)
    out = []
    for (section, name), rows_g in sorted(groups.items()):
        if len({r[0] for r in rows_g}) < 2:
            continue
        t = {k: sum(r[_mi(k)] for r in rows_g if isinstance(r[_mi(k)], int))
             for k in _SUM_KEYS}
        # a gym without delivery tracking contributes its sends to the
        # open-rate denominator; Delivered shows only the tracked part
        have_deliv = any(isinstance(r[I_DELIV], int) for r in rows_g)
        open_den = sum((r[I_DELIV] if isinstance(r[I_DELIV], int)
                        else r[I_SENDS]) for r in rows_g)
        zero = t["sends"] == 0

        def p(a, b):
            return 0 if zero else _pct(a, b)

        m = dict(t)
        m["delivered"] = t["delivered"] if have_deliv else ""
        for key, (num, den) in _RATIO_KEYS.items():
            m[key] = p(t[num], open_den if key in _DELIVERED_DEN_KEYS else t[den])
        m["note"] = ("no sends yet" if zero else
                     f"small sample (under {SMALL_N}), directional only"
                     if t["sends"] < SMALL_N else "")
        out.append([COMBINED, name] + [m[k] for k in METRIC_KEYS]
                   + ["automation", section, "", stamp, 0]
                   + [m[k] for k in _MAT_KEYS])
    return out


def _data_migrate(row: list, old_header: list | None) -> list:
    """A Data row read back under an older header (the other gym's cron still
    on a previous layout, or this one before a redeploy) is re-laid by column
    NAME into the current DATA_HEADER, blank where a column is new. Rows under
    the current header pass through. Added 2026-09-14 with the two 'opened
    first' columns; _normalize_row then rebuilds the numbers and GymOrder.
    A heading renamed on 2026-09-24 (RENAMED_HEADERS) is read under its new
    name, so the other gym's 60-day join numbers survive the rename."""
    if old_header and old_header != DATA_HEADER and "Gym" in old_header:
        return [_layout_vals(old_header, row, "GymOrder").get(h, "")
                for h in DATA_HEADER]
    return list(row)


def _layout_vals(old_header: list, row: list, last: str) -> dict:
    """heading -> value for a row read back under a header that is not the
    current one. Mixed-layout guard (fresh-eyes finding, 2026-09-24): the
    older writer clears only its own narrower width, so after it runs, the
    wider layout's columns from the day before are still at the right edge
    of the header row AND of every row. Only the columns up to the first
    `last` heading (the older layout's final column) are trusted; the stale
    tail is dropped (a maturity count read as blank = 0 until that gym's cron
    rewrites its rows), and a heading renamed on 2026-09-24 (RENAMED_HEADERS)
    keeps its value under the new name."""
    cut = old_header.index(last) + 1 if last in old_header else len(old_header)
    vals: dict = {}
    for h, v in zip(old_header[:cut], row[:cut]):
        h = str(h).strip()
        for pre in (HIST_MONTH_PREFIX, HIST_TOTAL_PREFIX, ""):
            if h.startswith(pre) and h[len(pre):] in RENAMED_HEADERS:
                h = pre + RENAMED_HEADERS[h[len(pre):]]
                break
        vals.setdefault(h, v)
    return vals


def _merge_data(svc, own_rows: list[dict], stamp: str) -> list[list]:
    """Replace this gym's Data rows, keep every other gym's, recompute the
    'All gyms' per-automation totals, rewrite the tab. The grid is grown to
    DATA_NCOLS first: the two Unsubscribe columns (2026-09-18) took the row
    past the 27 columns the live tab had, and a read, clear or write past the
    grid's edge is refused by Sheets."""
    _widen_tab(svc, "Data", DATA_NCOLS)
    got = svc.spreadsheets().values().get(
        spreadsheetId=ALLGYMS_SHEET_ID,
        range=f"Data!A{D0 - 1}:{_a1col(max(DATA_NCOLS, 26) - 1)}{D1}"
        ).execute(num_retries=_NUM_RETRIES).get("values", [])
    old_header = [str(x).strip() for x in (got[0] if got else [])]
    kept = [_normalize_row(_data_migrate(row, old_header)) for row in got[1:]
            if row and row[0]
            and row[0] not in (GYM, COMBINED, "All gyms")]  # "All gyms" =
    # the pre-2026-07-28 label of the combined rows; drop any leftovers
    per_gym = kept + [_data_row(r, stamp) for r in own_rows]
    merged = per_gym + _combined_rows(per_gym, stamp)
    # Context rows sort LAST inside their section (Luke 2026-08-07): they are
    # not arms, so they must not sit above the A/B rows a reader is trying to
    # compare. _fmt_requests greys them out to match.
    merged.sort(key=lambda r: (r[I_LEVEL], r[I_SECTION],
                               1 if r[1] in CONTEXT_LABELS else 0,
                               r[1], r[I_ORDER]))
    svc.spreadsheets().values().clear(
        spreadsheetId=ALLGYMS_SHEET_ID,
        range=f"Data!A:{_a1col(max(DATA_NCOLS, 26) - 1)}").execute(num_retries=_NUM_RETRIES)
    note = ("Machine-written by the Send It crons after every send run. "
            "Do not edit anything here; the Dashboard and Variants tabs "
            "read from this tab.")
    svc.spreadsheets().values().update(
        spreadsheetId=ALLGYMS_SHEET_ID, range="Data!A1",
        valueInputOption="RAW",
        body={"values": [[note], DATA_HEADER] + merged}).execute(num_retries=_NUM_RETRIES)
    return merged


def _gym_cond() -> str:
    return f'((($B$2="All gyms")+(Data!$A${D0}:$A${D1}=$B$2))>0)'


def _section_formula(level: str, section: str, variant_cols: bool) -> str:
    section = section.replace('"', '""')
    tag, note = _a1col(I_TAG), _col("note")
    lvl, sec = _a1col(I_LEVEL), _a1col(I_SECTION)
    if variant_cols:
        src = ("{Data!$A$%d:$B$%d,Data!$%s$%d:$%s$%d,Data!$%s$%d:$%s$%d}"
               % (D0, D1, tag, D0, tag, D1, _col("sends"), D0, note, D1))
    else:
        src = f"Data!$A${D0}:${note}${D1}"
    conds = ('Data!$%s$%d:$%s$%d="%s",Data!$%s$%d:$%s$%d="%s",%s'
             % (lvl, D0, lvl, D1, level, sec, D0, sec, D1, section,
                _gym_cond()))
    # No SORT wrapper: the writer pre-sorts Data into display order (name asc,
    # "All gyms" row first) and FILTER preserves it. SORT also breaks on
    # single-row results (a 1x1 sort-column arg parses as a column index).
    return '=IFERROR(FILTER(%s,%s),"no rows yet")' % (src, conds)


def _totals_formulas() -> list:
    """The "Everything combined" row: one formula per metric column, in
    METRIC_KEYS order, Note excluded (the row's label sits in column B)."""
    # excludes the "All gyms" per-automation rows or they would double-count
    lvl = _a1col(I_LEVEL)
    cond = (f'(Data!${lvl}${D0}:${lvl}${D1}="automation")'
            f'*(Data!$A${D0}:$A${D1}<>"{COMBINED}")*{_gym_cond()}')

    def sp(key):
        c = _col(key)
        return f"SUMPRODUCT({cond}*Data!${c}${D0}:${c}${D1})"

    # open-rate (and unsubscribe-rate) denominator falls back to sends where
    # delivered is blank (ABC)
    d, s = _col("delivered"), _col("sends")
    open_den = (f"SUMPRODUCT({cond}*IF(Data!${d}${D0}:${d}${D1}=\"\","
                f"Data!${s}${D0}:${s}${D1},Data!${d}${D0}:${d}${D1}))")

    out = []
    for key in METRIC_KEYS:
        if key == "note":
            continue
        if key in _DELIVERED_DEN_KEYS:
            out.append(f'=IFERROR({sp(_RATIO_KEYS[key][0])}/{open_den},"")')
        elif key in _RATIO_KEYS:
            num, den = _RATIO_KEYS[key]
            out.append(f'=IFERROR({sp(num)}/{sp(den)},"")')
        else:
            out.append(f"={sp(key)}")
    return out


def _build_stats_tab(tab: str, merged: list[list], stamp: str,
                     dropdown_value: str, variant_tab: bool) -> tuple:
    """Grid rows (None = leave empty for FILTER spill) + layout metadata."""
    level = "variant" if variant_tab else "automation"
    lead_cols = 3 if variant_tab else 2  # Gym, name (+Tag on Variants)
    ncols = lead_cols + len(METRIC_HEADERS)
    lvl_rows = [r for r in merged if r[I_LEVEL] == level]
    if variant_tab:
        counts: dict[str, int] = defaultdict(int)
        for r in lvl_rows:
            counts[r[I_SECTION]] += 1
        sections = sorted(counts, key=lambda s: -counts[s]) or ["A/B tests"]
    else:
        counts = defaultdict(int)
        for r in lvl_rows:
            counts[r[I_SECTION]] += 1
        sections = DASH_SECTIONS

    grid: list[list] = []
    meta = {"bands": [], "headers": [], "data_ranges": [], "notes": [],
            "totals": None, "ncols": ncols, "lead_cols": lead_cols,
            "variant_tab": variant_tab, "header_notes": []}
    gloss = VARIANT_DESCRIPTIONS if variant_tab else DESCRIPTIONS

    def _glossary(sec: str) -> str:
        seen, lines = set(), []
        for r in lvl_rows:
            name = r[1]
            if r[I_SECTION] != sec or name in seen or r[0] == COMBINED:
                continue
            seen.add(name)
            if gloss.get(name):
                lines.append(f"{name}: {gloss[name]}")
        return "\n\n".join(lines)
    title = ("Send It - All Gyms: every A/B arm, one row per email version"
             if variant_tab else
             "Send It - All Gyms: every automation, every gym")
    grid.append([f"{title}  (updated {stamp})"])
    grid.append(["Show:", dropdown_value or "All gyms"])
    grid.append([])
    head = (["Gym", "Email version", "Tag"] if variant_tab
            else ["Gym", "Automation"]) + METRIC_HEADERS

    def _pct_notes(row: int) -> None:
        # which emails are in each rate (Block 15 maturity), on every header
        for k, text in PCT_NOTES.items():
            meta["header_notes"].append(
                (row, lead_cols + METRIC_KEYS.index(k), text))
    if not variant_tab:
        grid.append(head)
        meta["headers"].append(len(grid))
        meta["header_notes"].append((len(grid), 0, DASH_HOWTO))
        _pct_notes(len(grid))
        meta["totals"] = len(grid) + 1
        grid.append([None, '="Everything combined ("&$B$2&")"']
                    + _totals_formulas())
        grid.append([])
    for sec in sections:
        grid.append([sec])
        meta["bands"].append(len(grid))
        grid.append(head)
        meta["headers"].append(len(grid))
        meta["header_notes"].append((len(grid), 1, _glossary(sec)))
        _pct_notes(len(grid))
        start = len(grid) + 1
        alloc = max(counts.get(sec, 0), 1)
        for i in range(alloc):
            row = [None] * ncols
            if i == 0:
                row[0] = _section_formula(level, sec, variant_tab)
            grid.append(row)
        meta["data_ranges"].append((start, start + alloc - 1))
        grid.append([])
    for line in FOOTNOTES:
        grid.append([line])
        meta["notes"].append(len(grid))
    return grid, meta


def _fmt_requests(sheet_id: int, meta: dict, existing_cf: int) -> list:
    ncols = meta["ncols"]
    left_cols = meta["lead_cols"]  # Gym/name (+Tag) stay left-aligned
    # 1-based display columns of every ratio metric
    pct_cols = [left_cols + METRIC_KEYS.index(k) + 1 for k in _RATIO_KEYS]
    first_band = meta["bands"][0]
    last_data = meta["data_ranges"][-1][1]
    reqs = [{"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": 0}}
            for _ in range(existing_cf)]
    # wipe ALL cell formatting + hover notes first: layouts shift between runs
    # and stale band/text formats or notes otherwise survive on rows that
    # moved (values().clear clears values only). Data validation survives.
    reqs.append({"repeatCell": {"range": {"sheetId": sheet_id}, "cell": {},
                 "fields": "userEnteredFormat,note"}})
    reqs.append({"updateSheetProperties": {
        "properties": {"sheetId": sheet_id,
                       "gridProperties": {"frozenRowCount": 2}},
        "fields": "gridProperties.frozenRowCount"}})

    def rng(r0, r1, c0=0, c1=None):
        return {"sheetId": sheet_id, "startRowIndex": r0 - 1, "endRowIndex": r1,
                "startColumnIndex": c0,
                "endColumnIndex": ncols if c1 is None else c1}

    def cell_fmt(r, fmt, fields):
        return {"repeatCell": {"range": r, "cell": {"userEnteredFormat": fmt},
                               "fields": ",".join(
                                   f"userEnteredFormat.{f}" for f in fields)}}

    reqs.append(cell_fmt(rng(1, 1), {"textFormat": {"bold": True, "fontSize": 12}},
                         ["textFormat"]))
    reqs.append(cell_fmt(rng(2, 2, 0, 1), {"textFormat": {"bold": True}},
                         ["textFormat"]))
    if meta["totals"]:
        t = meta["totals"]
        reqs.append(cell_fmt(
            rng(t, t), {"backgroundColor": _hex(HEADER_BG),
                        "textFormat": {"bold": True}},
            ["backgroundColor", "textFormat"]))
        reqs.append(cell_fmt(rng(t, t, left_cols),
                             {"horizontalAlignment": "CENTER"},
                             ["horizontalAlignment"]))
    for b in meta["bands"]:
        reqs.append(cell_fmt(
            rng(b, b), {"backgroundColor": _hex(BAND_BG),
                        "textFormat": {"bold": True,
                                       "foregroundColor": _hex(BAND_FG)}},
            ["backgroundColor", "textFormat"]))
    for h in meta["headers"]:
        reqs.append(cell_fmt(
            rng(h, h), {"backgroundColor": _hex(HEADER_BG),
                        "textFormat": {"bold": True},
                        "wrapStrategy": "WRAP",
                        "horizontalAlignment": "CENTER"},
            ["backgroundColor", "textFormat", "wrapStrategy",
             "horizontalAlignment"]))
    for s, e in meta["data_ranges"]:
        reqs.append(cell_fmt(rng(s, e, left_cols),
                             {"horizontalAlignment": "CENTER"},
                             ["horizontalAlignment"]))
        reqs.append(cell_fmt(rng(s, e, 0, left_cols),
                             {"horizontalAlignment": "LEFT"},
                             ["horizontalAlignment"]))
    for n in meta["notes"]:
        reqs.append(cell_fmt(
            rng(n, n), {"textFormat": {"italic": True,
                                       "foregroundColor": _hex(NOTE_FG)}},
            ["textFormat"]))
    fmt_top = meta["totals"] or first_band
    for c in pct_cols:
        reqs.append(cell_fmt(rng(fmt_top, last_data, c - 1, c),
                             {"numberFormat": {"type": "PERCENT",
                                               "pattern": "0%"}},
                             ["numberFormat"]))
    # note column wraps instead of overflowing
    reqs.append(cell_fmt(rng(first_band, last_data, ncols - 1, ncols),
                         {"wrapStrategy": "WRAP"}, ["wrapStrategy"]))
    # gym filter dropdown
    reqs.append({"setDataValidation": {
        "range": rng(2, 2, 1, 2),
        "rule": {"condition": {"type": "ONE_OF_LIST", "values": [
            {"userEnteredValue": v} for v in ["All gyms"] + list(GYM_FILLS)]},
            "strict": True, "showCustomUi": True}}})
    # gym color coding + alternating shading (conditional, tracks the filter).
    # The alt-shade rule must sit BEFORE the base rule (first match wins), so
    # rules are appended in that order (no index = append at the end).
    cf_range = rng(first_band, last_data)
    r0 = first_band
    # Context rows read as background, not as an arm (Luke 2026-08-07): grey
    # italic text. This rule is appended FIRST so its text format wins; it
    # sets no background, so the gym colour rules below still shade the row.
    # _merge_data has already sorted these rows to the bottom of their band.
    if meta["variant_tab"]:
        test = ",".join(f'$B{r0}="{lbl}"' for lbl in CONTEXT_LABELS)
        reqs.append({"addConditionalFormatRule": {"rule": {
            "ranges": [cf_range],
            "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA",
                              "values": [{"userEnteredValue":
                                          f"=OR({test})"}]},
                "format": {"textFormat": {
                    "italic": True,
                    "foregroundColor": _hex(PRETEST_FG)}}}}}})
    # "All gyms" per-automation total rows: bold on a neutral fill
    reqs.append({"addConditionalFormatRule": {"rule": {
        "ranges": [cf_range],
        "booleanRule": {
            "condition": {"type": "CUSTOM_FORMULA",
                          "values": [{"userEnteredValue":
                                      f'=$A{r0}="{COMBINED}"'}]},
            "format": {"backgroundColor": _hex("#e8eaed"),
                       "textFormat": {"bold": True}}}}}})
    for gym, (base, alt) in GYM_FILLS.items():
        for formula, color in (
                (f'=AND($A{r0}="{gym}",ISEVEN(ROW()))', alt),
                (f'=$A{r0}="{gym}"', base)):
            reqs.append({"addConditionalFormatRule": {"rule": {
                "ranges": [cf_range],
                "booleanRule": {
                    "condition": {"type": "CUSTOM_FORMULA",
                                  "values": [{"userEnteredValue": formula}]},
                    "format": {"backgroundColor": _hex(color)}}}}})
    lead = [70, 250, 210][:left_cols]
    widths = lead + [92] * (len(METRIC_HEADERS) - 1) + [340]  # last = Note
    for i, w in enumerate(widths):
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": w}, "fields": "pixelSize"}})
    return reqs


# History (recounted every run since 2026-09-18, Tasks tab step 1.1, Chris's
# OK 2026-09-17): one row per gym per month of SENDING, carrying two blocks of
# the same metric columns. "This month" = the emails SENT that month and what
# they earned, counted exactly as the Data tab counts (each person once per
# outcome, credited to the last email before the event, inside that email's
# window, plus "opened first"). "Running total" = everything sent from the
# first month through that month, counted the same way. Every month is rebuilt
# from the per-send records on every run, so a past month picks up late
# outcomes inside its windows and any counting fix (the 2026-09-17 Apple Mail
# opens fix corrected ABC's July and August opens this way). Before 2026-09-18
# a month was one cumulative snapshot frozen on its last run under whatever
# counting rule was live that day, so July and August read higher than they
# do now. The metric columns keep the Data row's order, so _mi() - 2 indexes
# a block.
HISTORY_METRIC_KEYS = [k for k in METRIC_KEYS if k != "note"]
_HIST_HEADERS = METRIC_HEADERS[:-1]
HIST_MONTH_PREFIX, HIST_TOTAL_PREFIX = "This month: ", "Running total: "
HISTORY_HEADER = (["Month", "Gym"]
                  + [HIST_MONTH_PREFIX + h for h in _HIST_HEADERS]
                  + [HIST_TOTAL_PREFIX + h for h in _HIST_HEADERS]
                  + ["Updated"])
HISTORY_NCOLS = len(HISTORY_HEADER)
_HIST_BLOCK = len(HISTORY_METRIC_KEYS)  # metric columns per block
_HIST_M0 = 2                             # first "This month" column (0-based)
_HIST_T0 = _HIST_M0 + _HIST_BLOCK        # first "Running total" column
_HIST_LASTCOL = _a1col(max(HISTORY_NCOLS, 26) - 1)
HISTORY_NOTES = {
    0: ("One row per gym per month. Every row is rebuilt on each run from the "
        "emails sent, so a month's numbers can still move until its outcome "
        "windows close (30 days for returns and redemptions, 60 and 90 for "
        "joins). Each % only counts the emails whose window has closed, as on "
        "the Dashboard. "
        "The current month is marked '(so far)'. Before 2026-09-18 each row "
        "was a frozen snapshot of the running total under the counting rule "
        "of that day."),
    _HIST_M0: ("This month = only the emails SENT in this month, and what those "
               "emails earned. Each person counts once per outcome, credited to "
               "the last email they got before they acted, inside that email's "
               "window (the Dashboard footnotes explain the windows)."),
    _HIST_T0: ("Running total = everything sent from the first month through "
               "this month, counted the same way. The current month's running "
               "total is the Dashboard's 'All' total for this gym."),
}


def _history_metrics(t: dict) -> list:
    """One History block from summed counts, with the Data row's ratio rules:
    Delivered blank (unknown) when nothing was tracked, Open % over delivered
    when it is known else over sends, a ratio blank when its denominator is 0,
    and plain 0s on a month with no sends."""
    zero = t["sends"] == 0
    m = dict(t)
    m["delivered"] = 0 if zero else (t["delivered"] or "")
    open_den = t["delivered"] or t["sends"]
    for key, (num, den) in _RATIO_KEYS.items():
        m[key] = 0 if zero else _pct(t[num], open_den if key in _DELIVERED_DEN_KEYS
                                     else t[den])
    return [m[k] for k in HISTORY_METRIC_KEYS]


def _months_through(first: str, last: str) -> list[str]:
    """Every YYYY-MM from first to last inclusive (empty months included, so
    a quiet month shows as a row of zeros rather than vanishing)."""
    y, mo = int(first[:4]), int(first[5:7])
    out = []
    while True:
        cur = f"{y:04d}-{mo:02d}"
        out.append(cur)
        if cur >= last:
            return out
        y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)


def _history_rows(records: list[dict], stamp: str,
                  month_now: str | None = None, today=None) -> list[list]:
    """This gym's History rows from collect()'s per-send records (SEND_RECORDS):
    one row per month from the first send through the current month, the
    current one labelled '(so far)'. A member-survey send (ftv False) counts on
    the send / delivered / open / click / response columns only, as its Data
    row does; the first-timer outcomes are blank there and are not summed.
    Each send goes through _bucket_add, the same call the Data rows use, so
    the rates are over mature emails only (Block 15)."""
    month_now = month_now or datetime.now(timezone.utc).strftime("%Y-%m")
    today = today or MATURE_ASOF[0] or datetime.now(timezone.utc).date()
    by_month: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    odd = 0
    for r in records:
        month = str(r.get("sent") or "")[:7]
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            odd += 1  # a hand-edited log row must not take the whole push down
            continue
        _bucket_add(by_month[month], r, today)
    if odd:
        print(f"  allgyms: History skipped {odd} send(s) with a non-ISO sent_date")
    if not by_month:
        return []
    months = sorted(by_month)
    running: dict[str, int] = defaultdict(int)
    out = []
    for m in _months_through(months[0], max(month_now, months[-1])):
        b = by_month.get(m) or {}
        this = {k: b.get(k, 0) for k in _SUM_KEYS}
        for k in _SUM_KEYS:
            running[k] += this[k]
        label = f"{m} (so far)" if m == month_now else m
        out.append([label, GYM] + _history_metrics(this)
                   + _history_metrics(dict(running)) + [stamp])
    return out


def _history_migrate(row: list, old_header: list | None = None) -> list:
    """Bring a row read back from the History tab up to the current layout.

    Since 2026-09-14 the tab's own header row is read back and each value is
    placed under the SAME heading it was written under, so the other gym's
    rows keep their numbers wherever new columns land. A row written before
    2026-09-18 was one cumulative snapshot under plain headings ("Sends"): it
    belongs in the Running total block, and its This month block reads blank
    until that gym's cron recounts it (both gyms deploy the same morning).
    Without a usable header the row is right-padded.
    """
    row = list(row)
    if old_header and old_header != HISTORY_HEADER and "Month" in old_header:
        # up to the older layout's own "Updated" only; renamed headings kept
        vals = _layout_vals(old_header, row, "Updated")
        out = []
        for h in HISTORY_HEADER:
            v = vals.get(h, "")
            if v == "" and h.startswith(HIST_TOTAL_PREFIX):
                v = vals.get(h[len(HIST_TOTAL_PREFIX):], "")
            out.append(v)
        return out
    return row + [""] * (HISTORY_NCOLS - len(row))


def _widen_tab(svc, title: str, ncols: int) -> None:
    """Grow a tab's grid to at least ncols columns. A values().clear or
    update past the grid's edge is refused by Sheets, and the History tab
    was 26 columns wide when the 41-column layout shipped (2026-09-18)."""
    meta = svc.spreadsheets().get(
        spreadsheetId=ALLGYMS_SHEET_ID,
        fields="sheets(properties(sheetId,title,gridProperties(columnCount)))"
        ).execute(num_retries=_NUM_RETRIES)
    for s in meta.get("sheets", []):
        p = s["properties"]
        have = p.get("gridProperties", {}).get("columnCount")
        if p.get("title") == title and have is not None and have < ncols:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=ALLGYMS_SHEET_ID,
                body={"requests": [{"appendDimension": {
                    "sheetId": p["sheetId"], "dimension": "COLUMNS",
                    "length": ncols - have}}]}).execute(num_retries=_NUM_RETRIES)
            print(f"  allgyms: '{title}' widened {have} -> {ncols} columns")


def _maintain_history(svc, records: list[dict], stamp: str) -> None:
    """Rewrite this gym's rows (every month, recounted from the per-send
    records) and keep the other gym's rows as they are. Values are read back
    UNFORMATTED so the other gym's numbers are rewritten as numbers, not as
    the '38%' text a formatted read produced before (2026-09-13 finding)."""
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    _widen_tab(svc, "History", HISTORY_NCOLS)
    got = svc.spreadsheets().values().get(
        spreadsheetId=ALLGYMS_SHEET_ID,
        range=f"History!A1:{_HIST_LASTCOL}1000",
        valueRenderOption="UNFORMATTED_VALUE",
        ).execute(num_retries=_NUM_RETRIES).get("values", [])
    old_header = [str(x).strip() for x in (got[0] if got else [])]
    kept = [_history_migrate(r, old_header) for r in got[1:]
            if r and r[0] and len(r) > 1 and str(r[1]).strip() != GYM]
    out = kept + _history_rows(records, stamp, month)
    out.sort(key=lambda r: (str(r[0]).split(" ")[0], str(r[1])))
    svc.spreadsheets().values().clear(
        spreadsheetId=ALLGYMS_SHEET_ID,
        range=f"History!A:{_HIST_LASTCOL}").execute(num_retries=_NUM_RETRIES)
    svc.spreadsheets().values().update(
        spreadsheetId=ALLGYMS_SHEET_ID, range="History!A1",
        valueInputOption="RAW",
        body={"values": [HISTORY_HEADER] + out}).execute(num_retries=_NUM_RETRIES)


def _history_fmt_requests(sheet_id: int, existing_cf: int) -> list:
    reqs = [{"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": 0}}
            for _ in range(existing_cf)]
    reqs.append({"repeatCell": {"range": {"sheetId": sheet_id}, "cell": {},
                 "fields": "userEnteredFormat"}})
    reqs.append({"updateSheetProperties": {
        "properties": {"sheetId": sheet_id,
                       "gridProperties": {"frozenRowCount": 1,
                                          "frozenColumnCount": 2}},
        "fields": "gridProperties.frozenRowCount,"
                  "gridProperties.frozenColumnCount"}})
    reqs.append({"repeatCell": {
        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
        "cell": {"userEnteredFormat": {"textFormat": {"bold": True},
                                       "backgroundColor": _hex(HEADER_BG),
                                       "wrapStrategy": "WRAP",
                                       "horizontalAlignment": "CENTER"}},
        "fields": "userEnteredFormat.textFormat,"
                  "userEnteredFormat.backgroundColor,"
                  "userEnteredFormat.wrapStrategy,"
                  "userEnteredFormat.horizontalAlignment"}})
    reqs.append({"repeatCell": {
        "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 1000,
                  "startColumnIndex": 2, "endColumnIndex": HISTORY_NCOLS},
        "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
        "fields": "userEnteredFormat.horizontalAlignment"}})
    # the same percent columns in both blocks (the Data row's 2-col lead
    # matches the This month block; the Running total block sits one block over)
    for c in _PCT_IDX:
        for off in (0, _HIST_BLOCK):
            reqs.append({"repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 1,
                          "endRowIndex": 1000, "startColumnIndex": c + off,
                          "endColumnIndex": c + off + 1},
                "cell": {"userEnteredFormat": {
                    "numberFormat": {"type": "PERCENT", "pattern": "0%"}}},
                "fields": "userEnteredFormat.numberFormat"}})
    # a rule between the two blocks and before Updated, so the eye finds
    # where This month ends and Running total starts
    for c in (_HIST_T0, HISTORY_NCOLS - 1):
        reqs.append({"updateBorders": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0,
                      "endRowIndex": 1000, "startColumnIndex": c,
                      "endColumnIndex": c + 1},
            "left": {"style": "SOLID_MEDIUM", "color": _hex(NOTE_FG)}}})
    for cidx, text in HISTORY_NOTES.items():
        reqs.append({"updateCells": {
            "rows": [{"values": [{"note": text}]}], "fields": "note",
            "start": {"sheetId": sheet_id, "rowIndex": 0, "columnIndex": cidx}}})
    for gym, (base, _alt) in GYM_FILLS.items():
        reqs.append({"addConditionalFormatRule": {"rule": {
            "ranges": [{"sheetId": sheet_id, "startRowIndex": 1,
                        "endRowIndex": 1000, "startColumnIndex": 0,
                        "endColumnIndex": HISTORY_NCOLS}],
            "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA",
                              "values": [{"userEnteredValue":
                                          f'=$B2="{gym}"'}]},
                "format": {"backgroundColor": _hex(base)}}}}})
    return reqs


# --------------------------------------------------------------------------
# Scorecard tab (ROADMAP Block 15, 2026-09-24; gym-agnostic, keep identical)
# --------------------------------------------------------------------------
# The Pilot scorecard from each gym's site, onto the sheet, so Chris reads the
# same numbers in both places: each cron writes its OWN gym's two rows (the
# pilot, and the same calendar dates a year earlier) from engine.
# build_scorecard, the exact call the site's Insights "Pilot scorecard" slide
# makes, and keeps the other gym's rows as they are (like History). The two
# questions stay apart on purpose (Luke 2026-09-24): the Dashboard is about
# EMAILS (clocked from the email), this tab is about PAID FIRST-TIMERS
# (clocked from the first paid visit), so the two cannot be the same number.
SCORECARD_TAB = "Scorecard"
SCORECARD_HEADER = [
    "Gym", "Group", "First paid visit between", "Paid first-timers",
    "Came back within 30 days", "Came back: out of (30 days passed)", "Came back %",
    "Goal: came back",
    "Joined within 30 days", "Joined: out of (30 days passed)", "Join % (30 days)",
    "Joined within 60 days", "Joined: out of (60 days passed)", "Join % (60 days)",
    "Joined within 90 days", "Joined: out of (90 days passed)", "Join % (90 days)",
    "Goal: joined (90 days)", "Data through", "Updated"]
SCORECARD_NCOLS = len(SCORECARD_HEADER)
_SC_LASTCOL = _a1col(max(SCORECARD_NCOLS, 26) - 1)
_SC_PCT_COLS = [SCORECARD_HEADER.index(h) for h in
                ("Came back %", "Join % (30 days)", "Join % (60 days)",
                 "Join % (90 days)")]
SCORECARD_NOTES = {
    0: ("Pilot scorecard: the same numbers as the 'Pilot scorecard' slide on "
        "each gym's Send It page, written by each gym's morning run. One row "
        "for the pilot so far and one for the same calendar dates a year "
        "earlier, counted the same way. The Dashboard tab is about emails; "
        "this tab is about paid first-timers, so the two are different "
        "questions and different numbers."),
    3: ("Paid first-timers: first paid entry was a day pass or a trial "
        "(SHIFT: Day Pass, 2-Week Trial, 30-Day Trial; ABC: day pass), not a "
        "youth pass, not staff, checked in at least once, first paid visit "
        "inside the dates shown, and would have passed the email screens (own "
        "email on file, not shared with another climber, did not join or buy "
        "a trial within 2 days)."),
    4: ("Came back = a check-in on a later day within 30 days of the first "
        "paid visit. Check-ins before that visit never count."),
    5: ("Only people whose 30 days have passed are in the bottom of the %, so "
        "someone who visited last week is never counted as a miss."),
    SCORECARD_HEADER.index("Joined within 30 days"): (
        "Joined = the first paid membership that is not a youth plan, started "
        "0 to 30 days after the first paid visit. Only people whose 30 days "
        "have passed are in the bottom of the %."),
    SCORECARD_HEADER.index("Joined within 60 days"): (
        "Same as the 30-day join, with 60 days."),
    SCORECARD_HEADER.index("Joined within 90 days"): (
        "Same as the 30-day join, with 90 days. The goal is set on this one. "
        "Only people whose 90 days have passed are in the bottom of the %."),
}


def _sc_ratio(n, of):
    """A Scorecard rate, NOT pre-rounded: the tab shows one decimal (0.0%), and
    rounding to 4 places first double-rounds (51 of 247 = 20.648% read 20.7%
    here while the site slide said 20.6%; simulation finding, 2026-09-24)."""
    return n / of if isinstance(n, int) and isinstance(of, int) and of else ""


def _scorecard_rows(sc: dict, stamp: str) -> list[list]:
    """This gym's two Scorecard rows from engine.build_scorecard's result
    (none when the gym has no scorecard switched on)."""
    if not sc or not sc.get("enabled"):
        return []
    goals = sc.get("goals") or {}
    out = []
    for pilot, part, lo, hi in (
            (True, sc["pilot"]["all"], sc["start"], sc["data_through"]),
            (False, sc["baseline"]["all"], sc["baseline_start"],
             sc["baseline_through"])):
        r, j60, j90 = part["ret"], part["join_early"], part["join"]
        j30 = part.get("join30") or {"n": "", "of": ""}
        out.append([
            GYM, "Pilot" if pilot else f"Same dates in {str(lo)[:4]}",
            f"{lo} to {hi}", part["n"],
            r["n"], r["of"], _sc_ratio(r["n"], r["of"]),
            goals.get("return", "") if pilot else "",
            j30["n"], j30["of"], _sc_ratio(j30["n"], j30["of"]),
            j60["n"], j60["of"], _sc_ratio(j60["n"], j60["of"]),
            j90["n"], j90["of"], _sc_ratio(j90["n"], j90["of"]),
            goals.get("join", "") if pilot else "",
            sc["data_through"], stamp])
    return out


def _maintain_scorecard(svc, sc: dict, stamp: str) -> None:
    """Rewrite this gym's Scorecard rows, keep every other gym's (read back
    unformatted and re-laid by header name, like History)."""
    _widen_tab(svc, SCORECARD_TAB, SCORECARD_NCOLS)
    got = svc.spreadsheets().values().get(
        spreadsheetId=ALLGYMS_SHEET_ID, range=f"{SCORECARD_TAB}!A1:{_SC_LASTCOL}100",
        valueRenderOption="UNFORMATTED_VALUE",
        ).execute(num_retries=_NUM_RETRIES).get("values", [])
    old_header = [str(x).strip() for x in (got[0] if got else [])]
    kept = []
    for r in got[1:]:
        if not r or not str(r[0]).strip() or str(r[0]).strip() == GYM:
            continue
        vals = dict(zip(old_header, r))
        kept.append([vals.get(h, "") for h in SCORECARD_HEADER])
    out = kept + _scorecard_rows(sc, stamp)
    out.sort(key=lambda r: (GYM_ORDER.get(str(r[0]), 9),
                            0 if str(r[1]) == "Pilot" else 1))
    svc.spreadsheets().values().clear(
        spreadsheetId=ALLGYMS_SHEET_ID,
        range=f"{SCORECARD_TAB}!A:{_SC_LASTCOL}").execute(num_retries=_NUM_RETRIES)
    svc.spreadsheets().values().update(
        spreadsheetId=ALLGYMS_SHEET_ID, range=f"{SCORECARD_TAB}!A1",
        valueInputOption="RAW",
        body={"values": [SCORECARD_HEADER] + out}).execute(num_retries=_NUM_RETRIES)


def _scorecard_fmt_requests(sheet_id: int, existing_cf: int) -> list:
    reqs = [{"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": 0}}
            for _ in range(existing_cf)]
    reqs.append({"repeatCell": {"range": {"sheetId": sheet_id}, "cell": {},
                 "fields": "userEnteredFormat,note"}})
    reqs.append({"updateSheetProperties": {
        "properties": {"sheetId": sheet_id,
                       "gridProperties": {"frozenRowCount": 1,
                                          "frozenColumnCount": 2}},
        "fields": "gridProperties.frozenRowCount,"
                  "gridProperties.frozenColumnCount"}})
    reqs.append({"repeatCell": {
        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
        "cell": {"userEnteredFormat": {"textFormat": {"bold": True},
                                       "backgroundColor": _hex(HEADER_BG),
                                       "wrapStrategy": "WRAP",
                                       "horizontalAlignment": "CENTER"}},
        "fields": "userEnteredFormat.textFormat,"
                  "userEnteredFormat.backgroundColor,"
                  "userEnteredFormat.wrapStrategy,"
                  "userEnteredFormat.horizontalAlignment"}})
    reqs.append({"repeatCell": {
        "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 100,
                  "startColumnIndex": 3, "endColumnIndex": SCORECARD_NCOLS},
        "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
        "fields": "userEnteredFormat.horizontalAlignment"}})
    for c in _SC_PCT_COLS:
        reqs.append({"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 100,
                      "startColumnIndex": c, "endColumnIndex": c + 1},
            "cell": {"userEnteredFormat": {
                "numberFormat": {"type": "PERCENT", "pattern": "0.0%"}}},
            "fields": "userEnteredFormat.numberFormat"}})
    for cidx, text in SCORECARD_NOTES.items():
        reqs.append({"updateCells": {
            "rows": [{"values": [{"note": text}]}], "fields": "note",
            "start": {"sheetId": sheet_id, "rowIndex": 0, "columnIndex": cidx}}})
    for i, w in enumerate([70, 150, 190] + [110] * (SCORECARD_NCOLS - 3)):
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": w}, "fields": "pixelSize"}})
    for gym, (base, _alt) in GYM_FILLS.items():
        reqs.append({"addConditionalFormatRule": {"rule": {
            "ranges": [{"sheetId": sheet_id, "startRowIndex": 1,
                        "endRowIndex": 100, "startColumnIndex": 0,
                        "endColumnIndex": SCORECARD_NCOLS}],
            "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA",
                              "values": [{"userEnteredValue": f'=$A2="{gym}"'}]},
                "format": {"backgroundColor": _hex(base)}}}}})
    return reqs


# --------------------------------------------------------------------------
# Experiments tab auto-fill (S40, gym-agnostic; keep identical in both repos)
# --------------------------------------------------------------------------
# The Experiments tab is HUMAN-OWNED (the port of Chris's Experiment Tracker;
# rebuilt tabs are Data/Dashboard/Variants/History ONLY, never this one).
# The cron writes ONLY Sample Size A/B + Result A/B on rows whose Send It
# Test dropdown names one of THIS gym's live tests (experiment_tests()).
# Untagged rows, other gyms' rows, and every other column are never touched.
# A test no longer in config = numbers freeze as the final record (silent
# skip). Fail-soft: any broken row prints a note that rides into the report
# email; nothing here can fail the stats push.

EXP_TAB = "Experiments"
EXP_METRIC_FIELDS = {
    "Open Rate": "opens",
    "Click Rate": "clicks",
    "Survey Completion Rate": "responses",
    "Second Visit Rate": "returned",
    "Membership Conversion Rate": "converted",
}
EXP_MANUAL_METRIC = "Retention Rate"  # not computable from Send It data


def _update_experiments(svc, tests: dict, var_rows: list[dict]) -> None:
    counts = {r["tag"]: r["m"] for r in var_rows}

    def arm(tags: list) -> dict:
        return {k: sum(int(counts.get(t, {}).get(k) or 0) for t in tags)
                for k in ("sends", "opens", "clicks", "responses",
                          "returned", "converted")}

    try:
        got = svc.spreadsheets().values().batchGet(
            spreadsheetId=ALLGYMS_SHEET_ID,
            ranges=[f"{EXP_TAB}!A1:Z1", f"{EXP_TAB}!A2:Z1000",
                    "Lists!A1:Z1000"]).execute(num_retries=_NUM_RETRIES)["valueRanges"]
    except Exception as exc:
        print(f"  allgyms: experiments auto-fill skipped ({exc})")
        return
    header = (got[0].get("values") or [[]])[0]
    rows = got[1].get("values") or []
    lists = got[2].get("values") or []

    need = ("Send It Test", "Success Metric", "Sample Size A",
            "Sample Size B", "Result A", "Result B")
    cols = {n: header.index(n) for n in need if n in header}
    if len(cols) < len(need):
        missing = ", ".join(n for n in need if n not in cols)
        print(f"  allgyms: experiments auto-fill skipped "
              f"(missing header(s): {missing})")
        return

    # every name ever on the Lists dropdown = a real test (maybe retired);
    # my-gym names outside it are typos worth a note
    known: set = set()
    if lists and "Send It Test" in lists[0]:
        li = lists[0].index("Send It Test")
        known = {r[li].strip() for r in lists[1:]
                 if len(r) > li and r[li].strip()}

    def cell(row: list, name: str) -> str:
        c = cols[name]
        return row[c].strip() if len(row) > c and row[c] else ""

    prefix = f"{GYM} - "
    data, notes, filled = [], [], 0
    for i, row in enumerate(rows):
        rn = i + 2
        test = cell(row, "Send It Test")
        if not test or not test.startswith(prefix):
            continue  # blank = fully manual row; otherwise another gym's
        if test not in tests:
            if test not in known:
                notes.append(f"row {rn}: unknown Send It Test "
                             f"'{test}', skipped")
            continue  # known but no longer active: numbers stay frozen
        metric = cell(row, "Success Metric")
        a, b = (arm(t) for t in tests[test])
        cells = {cols["Sample Size A"]: a["sends"],
                 cols["Sample Size B"]: b["sends"]}
        if metric in EXP_METRIC_FIELDS:
            f = EXP_METRIC_FIELDS[metric]
            cells[cols["Result A"]] = _pct(a[f], a["sends"])
            cells[cols["Result B"]] = _pct(b[f], b["sends"])
        elif metric != EXP_MANUAL_METRIC:
            notes.append(f"row {rn}: Success Metric "
                         f"'{metric or '(blank)'}' not auto-fillable, skipped")
            continue
        # Retention Rate: sample sizes still fill, Results stay manual
        for c, v in sorted(cells.items()):
            data.append({"range": f"{EXP_TAB}!{_a1col(c)}{rn}",
                         "values": [[v]]})
        filled += 1
    if data:
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=ALLGYMS_SHEET_ID,
            body={"valueInputOption": "RAW", "data": data}).execute(num_retries=_NUM_RETRIES)
    for n in notes:
        print(f"  allgyms: experiments {n}")
    print(f"  allgyms: experiments auto-fill: {filled} row(s) updated, "
          f"{len(notes)} skipped with notes")


def _update_experiments_v2(svc, client, records: list[dict], var_rows: list[dict],
                           sheet_id: int | None) -> None:
    """Experiments v2 (roadmap block 6, 2026-09-14): the gym's experiments.json
    drives the rows. Each entry is written DOWN into the row whose Experiment
    ID equals its sheet_id (appended if missing): the descriptive columns,
    Sample Size A/B = MATURE people per arm, Result A/B = the primary outcome
    rate per person (windowed, last touch), plus the V2 columns to the right
    of Chris's own (added to the header once, with hover notes). Rows the
    registry does not name are never touched. With no registry entries for
    this gym the S40 dropdown-text auto-fill above runs instead, so nothing
    regresses. Fail-soft like v1: the caller wraps this in try/except."""
    registry, notes = experiments.load_registry(client.client_dir)
    mine = [e for e in registry if e.get("gym") == GYM and e.get("active", True)]
    for n in notes:
        print(f"  allgyms: experiments {n}")
    if not mine:
        _update_experiments(svc, experiment_tests(client), var_rows)
        return
    got = svc.spreadsheets().values().get(
        spreadsheetId=ALLGYMS_SHEET_ID,
        range=f"{EXP_TAB}!A1:AZ1000").execute(num_retries=_NUM_RETRIES).get("values", [])
    header = [str(h).strip() for h in (got[0] if got else [])]
    rows = got[1:]
    if "Experiment ID" not in header:
        print("  allgyms: experiments v2 skipped (no 'Experiment ID' header)")
        return
    missing = [h for h in experiments.V2_HEADERS if h not in header]
    if missing:
        header = header + missing
        if sheet_id is not None:
            meta = svc.spreadsheets().get(
                spreadsheetId=ALLGYMS_SHEET_ID,
                fields="sheets(properties(sheetId,gridProperties(columnCount)))"
                ).execute(num_retries=_NUM_RETRIES)
            ncols = next((s["properties"]["gridProperties"]["columnCount"]
                          for s in meta.get("sheets", [])
                          if s["properties"]["sheetId"] == sheet_id), None)
            if ncols is not None and ncols < len(header):
                svc.spreadsheets().batchUpdate(
                    spreadsheetId=ALLGYMS_SHEET_ID,
                    body={"requests": [{"appendDimension": {
                        "sheetId": sheet_id, "dimension": "COLUMNS",
                        "length": len(header) - ncols}}]}).execute(num_retries=_NUM_RETRIES)
        svc.spreadsheets().values().update(
            spreadsheetId=ALLGYMS_SHEET_ID, range=f"{EXP_TAB}!A1",
            valueInputOption="RAW", body={"values": [header]}).execute(num_retries=_NUM_RETRIES)
        print(f"  allgyms: experiments v2 header gained {len(missing)} column(s)")
    col = {h: i for i, h in enumerate(header)}

    def cell(row: list, name: str) -> str:
        c = col.get(name)
        return row[c].strip() if c is not None and len(row) > c and row[c] else ""

    by_id: dict[str, int] = {}
    used = 1
    for i, row in enumerate(rows):
        v = cell(row, "Experiment ID")
        if v and v not in by_id:
            by_id[v] = i + 2
        if v or cell(row, "Send It Test"):
            used = i + 2
    next_free = used + 1
    today = datetime.now(timezone.utc).date()
    data, filled = [], 0
    for exp in mine:
        res = experiments.evaluate(exp, records, today)
        notes += res["notes"]
        rn = by_id.get(exp["sheet_id"])
        cells = dict(res["cells"])
        if rn is None:
            rn = next_free
            next_free += 1
            cells["Experiment ID"] = exp["sheet_id"]
            if exp.get("started"):
                cells["Date"] = exp["started"]
        for name, v in cells.items():
            c = col.get(name)
            if c is not None:
                data.append({"range": f"{EXP_TAB}!{_a1col(c)}{rn}", "values": [[v]]})
        filled += 1
        print(f"  allgyms: experiments {exp['sheet_id']} ({exp['exp_id']}): "
              f"{res['health']}; {res['result']}; {res['cells']['Progress']}")
    if data:
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=ALLGYMS_SHEET_ID,
            body={"valueInputOption": "RAW", "data": data}).execute(num_retries=_NUM_RETRIES)
    if sheet_id is not None:
        note_reqs = [{"updateCells": {
            "rows": [{"values": [{"note": experiments.V2_NOTES[h]}]}], "fields": "note",
            "start": {"sheetId": sheet_id, "rowIndex": 0, "columnIndex": col[h]}}}
            for h in experiments.V2_HEADERS if h in col]
        if note_reqs:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=ALLGYMS_SHEET_ID,
                body={"requests": note_reqs}).execute(num_retries=_NUM_RETRIES)
    for n in notes:
        print(f"  allgyms: experiments {n}")
    print(f"  allgyms: experiments v2: {filled} row(s) written from the registry")


def push(slug: str = "shift") -> None:
    client = config.load_client(slug)
    auto_rows, var_rows = collect(client)
    reclaim()  # collect's ingest + feed garbage, before the Sheets work starts
    stamp = _stamp()
    svc = _sheets_service()

    fields = "sheets(properties(sheetId,title),conditionalFormats)"
    meta0 = svc.spreadsheets().get(
        spreadsheetId=ALLGYMS_SHEET_ID, fields=fields).execute(num_retries=_NUM_RETRIES)
    have = {s["properties"]["title"] for s in meta0["sheets"]}
    add = [t for t in ("Dashboard", SCORECARD_TAB, "Variants", "History", "Data")
           if t not in have]
    if add:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=ALLGYMS_SHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": t}}}
                               for t in add]}).execute(num_retries=_NUM_RETRIES)
        meta0 = svc.spreadsheets().get(
            spreadsheetId=ALLGYMS_SHEET_ID, fields=fields).execute(num_retries=_NUM_RETRIES)
    sheet_ids = {s["properties"]["title"]: s["properties"]["sheetId"]
                 for s in meta0["sheets"]}
    cf_counts = {s["properties"]["title"]: len(s.get("conditionalFormats", []))
                 for s in meta0["sheets"]}

    # keep the user's current dropdown picks across the rewrite
    picks = {"Dashboard": "All gyms", "Variants": "All gyms"}
    try:
        got = svc.spreadsheets().values().batchGet(
            spreadsheetId=ALLGYMS_SHEET_ID,
            ranges=["Dashboard!B2", "Variants!B2"]).execute(num_retries=_NUM_RETRIES)
        for tab, vr in zip(("Dashboard", "Variants"), got["valueRanges"]):
            v = (vr.get("values") or [[""]])[0][0]
            if v in ("All gyms",) + tuple(GYM_FILLS):
                picks[tab] = v
    except Exception:
        pass

    stage("sheet metadata read")
    merged = _merge_data(svc, auto_rows + var_rows, stamp)
    print(f"  allgyms: Data tab now {len(merged)} rows "
          f"({len(auto_rows)} automations + {len(var_rows)} variants from {GYM})")
    stage(f"Data tab rewritten ({len(merged)} rows)")
    reclaim()

    fmt_reqs: list = []
    for tab, variant_tab in (("Dashboard", False), ("Variants", True)):
        grid, m = _build_stats_tab(tab, merged, stamp, picks[tab], variant_tab)
        # the two 90-day join columns (2026-09-24) took Variants past column
        # Z: grow the grid first, or the FILTER spill has nowhere to land
        _widen_tab(svc, tab, m["ncols"])
        svc.spreadsheets().values().clear(
            spreadsheetId=ALLGYMS_SHEET_ID,
            range=f"{tab}!A:{_a1col(max(m['ncols'], 26) - 1)}").execute(num_retries=_NUM_RETRIES)
        svc.spreadsheets().values().update(
            spreadsheetId=ALLGYMS_SHEET_ID, range=f"{tab}!A1",
            valueInputOption="USER_ENTERED", body={"values": grid}).execute(num_retries=_NUM_RETRIES)
        fmt_reqs += _fmt_requests(sheet_ids[tab], m, cf_counts.get(tab, 0))
        for r, cidx, text in m["header_notes"]:
            if text:
                fmt_reqs.append({"updateCells": {
                    "rows": [{"values": [{"note": text}]}], "fields": "note",
                    "start": {"sheetId": sheet_ids[tab], "rowIndex": r - 1,
                              "columnIndex": cidx}}})
        print(f"  allgyms: rebuilt '{tab}' "
              f"({len(m['bands'])} sections, filter = {picks[tab]})")
        stage(f"'{tab}' tab rewritten ({len(grid)} rows)")
        reclaim()

    _maintain_history(svc, SEND_RECORDS, stamp)
    stage("History tab maintained (every month recounted)")
    reclaim()
    fmt_reqs += _history_fmt_requests(sheet_ids["History"],
                                      cf_counts.get("History", 0))
    # Scorecard tab (Block 15): guarded so it can never cost the rest of the
    # push, and loud when it breaks (CLAUDE.md rule 5)
    try:
        if SCORECARD.get("enabled"):
            _maintain_scorecard(svc, SCORECARD, stamp)
            fmt_reqs += _scorecard_fmt_requests(sheet_ids[SCORECARD_TAB],
                                                cf_counts.get(SCORECARD_TAB, 0))
            stage("Scorecard tab maintained")
        else:
            # no scorecard this run (switched off, or its build FAILED above):
            # this gym's rows stay exactly as the last good run wrote them
            stage("Scorecard tab: no scorecard this run, rows untouched")
    except Exception:
        print("  allgyms: Scorecard tab FAILED (stats push unaffected):\n"
              + traceback.format_exc())
        stage("Scorecard tab FAILED (stats push unaffected)")
    reclaim()
    # Data tab: format reset + freeze + light header styling
    did = sheet_ids["Data"]
    fmt_reqs += [
        {"repeatCell": {"range": {"sheetId": did}, "cell": {},
                        "fields": "userEnteredFormat"}},
        {"updateSheetProperties": {
            "properties": {"sheetId": did,
                           "gridProperties": {"frozenRowCount": 2}},
            "fields": "gridProperties.frozenRowCount"}},
        {"repeatCell": {
            "range": {"sheetId": did, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {"textFormat": {
                "italic": True, "foregroundColor": _hex(NOTE_FG)}}},
            "fields": "userEnteredFormat.textFormat"}},
        {"repeatCell": {
            "range": {"sheetId": did, "startRowIndex": 1, "endRowIndex": 2},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
            "fields": "userEnteredFormat.textFormat"}},
    ]
    # tab order: Dashboard, Scorecard, Variants, History, Data
    for i, t in enumerate(("Dashboard", SCORECARD_TAB, "Variants", "History",
                           "Data")):
        fmt_reqs.append({"updateSheetProperties": {
            "properties": {"sheetId": sheet_ids[t], "index": i},
            "fields": "index"}})
    svc.spreadsheets().batchUpdate(
        spreadsheetId=ALLGYMS_SHEET_ID,
        body={"requests": fmt_reqs}).execute(num_retries=_NUM_RETRIES)
    print(f"  allgyms: formatting applied, History maintained ({stamp})")
    stage(f"formatting applied ({len(fmt_reqs)} requests)")
    reclaim()

    try:
        _update_experiments_v2(svc, client, SEND_RECORDS, var_rows,
                               sheet_ids.get(EXP_TAB))
        stage("experiments auto-fill done")
    except Exception:
        print("  allgyms: experiments auto-fill FAILED (stats push "
              "unaffected):\n" + traceback.format_exc())
        stage("experiments auto-fill FAILED (stats push unaffected)")


if __name__ == "__main__":
    push("shift")
