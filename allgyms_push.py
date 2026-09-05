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
  History   - one row per gym per month (cumulative snapshot); the current
              month rides as "(so far)" and is finalized on month rollover.

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
import sys
import traceback
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from nudge_tool import config, drive_io, engine, ingest, survey
from nudge_tool.mailchimp_client import MailchimpClient
from nudge_tool.stage import stage as _stage


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
CACHE_FIELDS = ["email", "sent_date", "tag", "delivered", "opened", "clicked",
                "frozen_at"]

# trigger_name -> display label (falls back to the raw name)
FUNNEL_LABELS = {
    "survey_request": "FTV survey (day 1-2)",
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
}
# Order here is the order the Dashboard renders sections in. KEEP THIS LIST
# IDENTICAL IN BOTH REPOS. Each gym's cron rebuilds the whole Dashboard from
# its OWN copy, and ABC's cron runs at 11:00 while SHIFT's runs at 11:03, so a
# section that exists in only one repo is written at 11:03 and erased the next
# morning at 11:00 (or vice versa) with no error anywhere.
DASH_SECTIONS = ["FTV funnel emails", "Day pass regulars", "Blocker nudges",
                 "Surveys"]


def _section_of(trigger_name: str) -> str:
    if trigger_name == "survey_request":
        return "Surveys"
    if trigger_name.startswith("nudge_") and trigger_name != "nudge_round_two":
        return "Blocker nudges"
    # Day-pass regulars are NOT first-time visitors (3-10 visits each), so they
    # do not belong under the FTV funnel heading. SHIFT-only today; the section
    # simply renders empty for a gym that has no such trigger active.
    if trigger_name == "daypass_to_trial":
        return "Day pass regulars"
    return "FTV funnel emails"


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
}
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
    "Q1 taps",
    "Responses", "Response %", "Offer redemptions", "Purchases after send",
    "Returned after send", "Return %", "Converted after send", "Conversion %",
    "Note"]
METRIC_KEYS = [
    "sends", "delivered", "opens", "open_pct", "clicks", "cpo_pct",
    "taps",
    "responses", "resp_pct", "redeems", "purchases",
    "returned", "return_pct", "converted", "conv_pct", "note"]

# Data tab layout: row 1 = do-not-edit note, row 2 = header, rows 3+ = data.
# Cols: A Gym, B Name, then one column per METRIC_HEADERS entry, then
# Level, Section, Tag, Updated, GymOrder (sort key so "All gyms" rows sit
# above gym rows).
DATA_HEADER = (["Gym", "Automation / email version"] + METRIC_HEADERS
               + ["Level", "Section", "Tag", "Updated", "GymOrder"])
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


def _mi(key: str) -> int:
    """0-based index of a metric column in a Data row."""
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
_COUNT_KEYS = ("sends", "delivered", "opens", "clicks", "taps", "responses",
               "redeems", "purchases", "returned", "converted")
# ratio metric -> (numerator key, denominator key). Open % swaps its
# denominator to sends when a gym has no delivery tracking (see _totals_formulas).
_RATIO_KEYS = {
    "open_pct": ("opens", "delivered"),
    "cpo_pct": ("clicks", "opens"),
    "resp_pct": ("responses", "sends"),
    "return_pct": ("returned", "sends"),
    "conv_pct": ("converted", "sends"),
}
_COUNT_IDX = [_mi(k) for k in _COUNT_KEYS]
_PCT_IDX = [_mi(k) for k in _RATIO_KEYS]
I_SENDS, I_DELIV, I_OPENS = _mi("sends"), _mi("delivered"), _mi("opens")
I_NOTE = _mi("note")
I_LEVEL, I_SECTION, I_TAG = I_NOTE + 1, I_NOTE + 2, I_NOTE + 3
I_UPDATED, I_ORDER = I_NOTE + 4, I_NOTE + 5
DATA_NCOLS = I_ORDER + 1
DATA_LASTCOL = _a1col(I_ORDER)

FOOTNOTES = [
    "Why these numbers can differ from Mailchimp's screens: Mailchimp still "
    "counts old test sends that can't be removed, and it counts an 'open' even "
    "when the open belonged to a different email. This sheet only counts real "
    "climbers and pins every open to the exact email we sent. Trust this sheet "
    "and the Send It dashboards.",
    "Why they can differ from Beta/RGP screens too: those systems count all "
    "visitors and sales all day. This sheet only looks at people we emailed, "
    "and only at what they did after the email.",
    "Apple devices auto-open emails, which inflates open rates a little, "
    "equally for every version, so comparisons stay fair.",
    "Offer redemptions: SHIFT counts a day pass bought at 50% or more off "
    "after the offer email (Beta never records which coupon was used). ABC "
    "counts the 'email discount' product (or an under-$8.25 day pass) on RGP "
    "invoices.",
    "Delivered: SHIFT counts Mailchimp's send confirmation, ABC counts "
    "Brevo's delivered receipt. A blank Delivered means that system had no "
    "delivery info for those emails, and that row's Open % is out of sends.",
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
    from googleapiclient.discovery import build
    creds = drive_io._creds("https://www.googleapis.com/auth/spreadsheets")
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


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
    """(email, sent_date, tag) -> (delivered, opened, clicked).

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
                            r.get("clicked") == "1")
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
            for (email, sent, tag), (d, o, c) in sorted(measured.items()):
                w.writerow({"email": email, "sent_date": sent, "tag": tag,
                            "delivered": int(d), "opened": int(o),
                            "clicked": int(c), "frozen_at": stamp})
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


def collect(client) -> tuple[list[dict], list[dict]]:
    """This gym's stats -> (automation rows, variant rows), one dict each:
    {gym, name, section, tag, level, m: {metric_key: value}}"""
    rows = _pull_outreach(BASE / "_allgyms_outreach_snapshot.csv")
    stage(f"outreach snapshot pulled ({len(rows)} rows)")
    ds = ingest.load(client)
    stage(f"ingest.load done ({len(ds.climbers)} climbers)")
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
        older ones only when the cache has no answer for them yet."""
        return (s["sent"] >= fresh_from
                or (s["email"], s["sent"], s["tag"]) not in cache)

    need = [s for s in sends if _is_fresh(s)]
    emails = sorted({s["email"] for s in need})
    stage(f"activity feeds: {len(emails)} to fetch "
          f"({len(need)} of {len(sends)} sends live, "
          f"{len(sends) - len(need)} from cache, "
          f"window={ENGAGEMENT_FRESH_DAYS}d from {fresh_from}, "
          f"cache={'on' if ENGAGEMENT_CACHE_DRIVE_ID else 'OFF'})")
    feeds = {}
    for i, e in enumerate(emails, 1):
        feeds[e] = mc.member_activity(e)
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

    click_markers = engine._SURVEY_LINK_MARKERS + engine._BUY_LINK_MARKERS
    by_trig: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    by_tag: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    trig_of_tag: dict[str, str] = {}
    measured: dict[tuple, tuple] = {}  # what this run knows -> the next cache
    for s in sends:
        email, sent = s["email"], s["sent"]
        key = (email, sent, s["tag"])
        hit = cache.get(key) if not _is_fresh(s) else None
        if hit is not None:
            # settled send: keep the delivered/opened/clicked already measured
            delivered, opened, clicked = hit
        else:
            ev = feeds.get(email) or []
            sent_ids = engine._sent_campaign_ids(ev, sent, journey_ids)
            delivered = bool(sent_ids)
            opened = delivered and engine._has_event(
                ev, "open", sent, campaign_ids=sent_ids)
            clicked = engine._has_event(ev, "click", sent, click_markers)
        measured[key] = (delivered, opened, clicked)
        c = climber_by_email.get(email)
        returned = bool(c and any(v > sent for v in c.visit_days))
        converted = bool(c and getattr(c, "membership_created", "")
                         and c.membership_created >= sent)
        redeemed = bool(c and any(v > sent for v in c.discounted_daypass_dates))
        txs = (tx_by_cid.get(c.climber_id, []) if c else []) \
            + tx_by_email.get(email, [])
        purchased = any(t > sent for t in txs)
        responded = email in resp_by_email and resp_by_email[email] >= sent
        tapped = s["tag"] in EMBED_TAGS and email in taps_valid
        for bucket, key in ((by_trig, s["trig"] or s["tag"]), (by_tag, s["tag"])):
            b = bucket[key]
            b["sends"] += 1
            b["delivered"] += delivered
            b["opens"] += opened
            b["clicks"] += clicked
            b["taps"] += tapped
            b["responses"] += responded
            b["redeems"] += redeemed
            b["purchases"] += purchased
            b["returned"] += returned
            b["converted"] += converted
        trig_of_tag[s["tag"]] = s["trig"] or s["tag"]

    # zero-rows (Luke 2026-07-28): every ACTIVE automation and every test arm
    # shows on the sheet even before its first send, with 0 values. Inactive
    # triggers (trial win-back, day pass to trial) stay off the sheet.
    for t in client.triggers:
        if t.active:
            by_trig[t.name]
    for tag in TEST_ARMS:
        by_tag[tag]

    def metrics(b: dict) -> dict:
        m = {k: b[k] for k in ("sends", "delivered", "opens", "clicks", "taps",
                               "responses", "redeems", "purchases",
                               "returned", "converted")}
        zero = b["sends"] == 0
        m["open_pct"] = 0 if zero else _pct(b["opens"], b["delivered"])
        # Chris's Variants!H5 ask (2026-08-06): of the people who opened, how
        # many clicked. Blank when nobody opened (_pct returns "" on a zero
        # denominator), never 0, so an unopened row can't read as "0% click".
        m["cpo_pct"] = 0 if zero else _pct(b["clicks"], b["opens"])
        m["resp_pct"] = 0 if zero else _pct(b["responses"], b["sends"])
        m["return_pct"] = 0 if zero else _pct(b["returned"], b["sends"])
        m["conv_pct"] = 0 if zero else _pct(b["converted"], b["sends"])
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
               GYM_ORDER.get(r["gym"], 9)])


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
             for k in _COUNT_KEYS}
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
            m[key] = p(t[num], open_den if key == "open_pct" else t[den])
        m["note"] = ("no sends yet" if zero else
                     f"small sample (under {SMALL_N}), directional only"
                     if t["sends"] < SMALL_N else "")
        out.append([COMBINED, name] + [m[k] for k in METRIC_KEYS]
                   + ["automation", section, "", stamp, 0])
    return out


def _merge_data(svc, own_rows: list[dict], stamp: str) -> list[list]:
    """Replace this gym's Data rows, keep every other gym's, recompute the
    'All gyms' per-automation totals, rewrite the tab."""
    got = svc.spreadsheets().values().get(
        spreadsheetId=ALLGYMS_SHEET_ID,
        range=f"Data!A{D0}:{DATA_LASTCOL}{D1}").execute(num_retries=_NUM_RETRIES).get("values", [])
    kept = [_normalize_row(row) for row in got
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
        spreadsheetId=ALLGYMS_SHEET_ID, range="Data!A:Z").execute(num_retries=_NUM_RETRIES)
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

    # open-rate denominator falls back to sends where delivered is blank (ABC)
    d, s = _col("delivered"), _col("sends")
    open_den = (f"SUMPRODUCT({cond}*IF(Data!${d}${D0}:${d}${D1}=\"\","
                f"Data!${s}${D0}:${s}${D1},Data!${d}${D0}:${d}${D1}))")

    out = []
    for key in METRIC_KEYS:
        if key == "note":
            continue
        if key == "open_pct":
            out.append(f'=IFERROR({sp("opens")}/{open_den},"")')
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
    if not variant_tab:
        grid.append(head)
        meta["headers"].append(len(grid))
        meta["header_notes"].append((len(grid), 0, DASH_HOWTO))
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


# History carries every metric column except Note; its rows are
# [Month, Gym] + metrics, the same 2-column lead as a Data row, so _mi()
# indexes both.
HISTORY_METRIC_KEYS = [k for k in METRIC_KEYS if k != "note"]
HISTORY_HEADER = (["Month", "Gym"] + METRIC_HEADERS[:-1] + ["Updated"])
HISTORY_NCOLS = len(HISTORY_HEADER)


def _history_totals(auto_rows: list[dict], stamp: str) -> list:
    t = defaultdict(int)
    deliv_known = False
    for r in auto_rows:
        for k in _COUNT_KEYS:
            v = r["m"][k]
            if isinstance(v, int):
                t[k] += v
                if k == "delivered":
                    deliv_known = True
    m = dict(t)
    m["delivered"] = t["delivered"] if deliv_known else ""
    open_den = t["delivered"] if deliv_known else t["sends"]
    for key, (num, den) in _RATIO_KEYS.items():
        m[key] = _pct(t[num], open_den if key == "open_pct" else t[den])
    return [GYM] + [m[k] for k in HISTORY_METRIC_KEYS] + [stamp]


def _history_migrate(row: list) -> list:
    """Bring a row read back from the History tab up to the current layout.

    Adding "Clicks per open %" (2026-08-13) inserted a column mid-row. Rows
    written by an earlier build are one cell short, and blindly right-padding
    them would slide Q1 taps under the new header and every metric after it one
    column left. A short row instead gets a blank inserted AT the new column, so
    finalized months keep their real numbers and simply read blank for a metric
    that was never measured. Rows already at full width pass through untouched.
    """
    row = list(row)
    if len(row) < HISTORY_NCOLS:
        row.insert(_mi("cpo_pct"), "")
    return row + [""] * (HISTORY_NCOLS - len(row))


def _maintain_history(svc, auto_rows: list[dict], stamp: str) -> None:
    """One row per gym per month. The current month rides as '(so far)' and is
    finalized (label loses the suffix) on the first run of the next month."""
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    got = svc.spreadsheets().values().get(
        spreadsheetId=ALLGYMS_SHEET_ID,
        range=f"History!A2:{_a1col(HISTORY_NCOLS - 1)}1000"
        ).execute(num_retries=_NUM_RETRIES).get("values", [])
    rows = [_history_migrate(r) for r in got if r and r[0]]
    out = []
    for r in rows:
        m, g = r[0], r[1]
        if g == GYM and m.endswith("(so far)"):
            base = m.split(" ")[0]
            if base == month:
                continue  # replaced by the fresh stub below
            final_exists = any(x[0] == base and x[1] == GYM for x in rows)
            if not final_exists:
                out.append([base] + r[1:])  # month rolled over: finalize
            continue
        out.append(r)
    out.append([f"{month} (so far)"] + _history_totals(auto_rows, stamp))
    out.sort(key=lambda r: (r[0].split(" ")[0], r[1]))
    svc.spreadsheets().values().clear(
        spreadsheetId=ALLGYMS_SHEET_ID, range="History!A:Z").execute(num_retries=_NUM_RETRIES)
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
                       "gridProperties": {"frozenRowCount": 1}},
        "fields": "gridProperties.frozenRowCount"}})
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
    for c in _PCT_IDX:  # 0-based; History shares the Data row's 2-col lead
        reqs.append({"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1,
                      "endRowIndex": 1000, "startColumnIndex": c,
                      "endColumnIndex": c + 1},
            "cell": {"userEnteredFormat": {
                "numberFormat": {"type": "PERCENT", "pattern": "0%"}}},
            "fields": "userEnteredFormat.numberFormat"}})
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


def push(slug: str = "shift") -> None:
    client = config.load_client(slug)
    auto_rows, var_rows = collect(client)
    stamp = _stamp()
    svc = _sheets_service()

    fields = "sheets(properties(sheetId,title),conditionalFormats)"
    meta0 = svc.spreadsheets().get(
        spreadsheetId=ALLGYMS_SHEET_ID, fields=fields).execute(num_retries=_NUM_RETRIES)
    have = {s["properties"]["title"] for s in meta0["sheets"]}
    add = [t for t in ("Dashboard", "Variants", "History", "Data")
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

    fmt_reqs: list = []
    for tab, variant_tab in (("Dashboard", False), ("Variants", True)):
        grid, m = _build_stats_tab(tab, merged, stamp, picks[tab], variant_tab)
        svc.spreadsheets().values().clear(
            spreadsheetId=ALLGYMS_SHEET_ID, range=f"{tab}!A:Z").execute(num_retries=_NUM_RETRIES)
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

    _maintain_history(svc, auto_rows, stamp)
    stage("History tab maintained")
    fmt_reqs += _history_fmt_requests(sheet_ids["History"],
                                      cf_counts.get("History", 0))
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
    # tab order: Dashboard, Variants, History, Data
    for i, t in enumerate(("Dashboard", "Variants", "History", "Data")):
        fmt_reqs.append({"updateSheetProperties": {
            "properties": {"sheetId": sheet_ids[t], "index": i},
            "fields": "index"}})
    svc.spreadsheets().batchUpdate(
        spreadsheetId=ALLGYMS_SHEET_ID,
        body={"requests": fmt_reqs}).execute(num_retries=_NUM_RETRIES)
    print(f"  allgyms: formatting applied, History maintained ({stamp})")
    stage(f"formatting applied ({len(fmt_reqs)} requests)")

    try:
        _update_experiments(svc, experiment_tests(client), var_rows)
        stage("experiments auto-fill done")
    except Exception:
        print("  allgyms: experiments auto-fill FAILED (stats push "
              "unaffected):\n" + traceback.format_exc())
        stage("experiments auto-fill FAILED (stats push unaffected)")


if __name__ == "__main__":
    push("shift")
