"""Experiments v2: the registry and the stats behind Chris's Experiments tab
(roadmap block 6, phases A + C, 2026-09-14).

What replaced what. Until now the cron matched Experiments rows by the text in
the "Send It Test" dropdown and wrote only Sample Size A/B (sends per arm) and
Result A/B (a per-send rate), and Chris's own formula called a winner when both
arms had 100 sends and the lift was 30% or more. Now each gym ships an
`experiments.json` in its client config (Luke owns it, decision 1). The cron
reads it as the truth, writes the descriptive columns DOWN into the row whose
Experiment ID matches, and fills the measurement columns from these rules:

  * people, not sends: one person per arm, in the arm their sends' tags say;
  * the primary outcome is per person, windowed, last touch (see
    attribution.py); opened/clicked outcomes come straight from the send;
  * only MATURE people count (their first arm send is older than the outcome
    window), so a test never judges emails that have not had time;
  * needed per arm from baseline + minimum worthwhile effect (alpha 0.05
    two-sided, power 0.8), a two-proportion test with a 95% CI on the lift,
    a split check (SRM) on everyone randomised, and an honest Health /
    Progress / Weeks-to-readable trio instead of a blank (decision 4).

Bucket tests (survey reminder test, Tasks step 2.2, 2026-09-23). Some tests
have an arm that sends NOTHING (A = no reminder), so its people can never be
found by a tag. A registry entry with a `bucket` names the hash salt and a
`population` names the sends that put a person in the test:

    "bucket": "survey_reminder_ab",
    "population": {"tags": ["FTV_survey_subject_a", ...], "since": "2026-10-01"},
    "arms": {"A": [], "B": ["FTV_survey_reminder_embed"]}

Everyone with a population send on or after `since` is in the test; their arm
is the deterministic hash of email + bucket name (the same hash the engine's
`ab_bucket` requirement uses to decide who gets the B email, so the two can
never disagree). Sends whose tag is listed under an arm (the reminder) ride
along on that person's record list, so a response credited to the reminder
still counts for the person. The outcome clock is the person's FIRST
population send (both arms judged "within N days of the first email").

Pure functions plus one JSON reader; the sheet writer lives in
allgyms_push.py. Shared by both gyms (CLAUDE.md rule 4): keep this file
byte-identical in ABC/Automation and SHIFT/SHIFT_Automation.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, timedelta
from pathlib import Path

from nudge_tool.attribution import WINDOWS as CREDIT_WINDOWS

# outcome key -> (plain label, base flag on the send record, window days)
# converted_30d (2026-09-18, Tasks tab step 1.2, Chris's 9/16 comment): the
# membership-vs-trial test is judged on joining within 30 days of the
# membership offer email, not 60. The credit on the record is still windowed
# at attribution.WINDOWS (60 days for a join), so a tighter outcome also
# checks the event date carried on the record (`converted_at`).
OUTCOMES = {
    "opened_7d": ("Opened the email within 7 days", "opened", 7),
    "clicked_7d": ("Clicked a link within 7 days", "clicked", 7),
    "responded_14d": ("Answered the survey within 14 days", "responded", 14),
    "returned_30d": ("Came back within 30 days", "returned", 30),
    "converted_30d": ("Joined within 30 days", "converted", 30),
    "converted_60d": ("Joined within 60 days", "converted", 60),
    "redeemed_30d": ("Used the offer within 30 days", "redeemed", 30),
}
DEFAULT_OUTCOME = "returned_30d"
DEFAULT_MWE = 0.05          # 5 percentage points
Z_ALPHA = 1.959964          # two-sided 0.05
Z_POWER = 0.841621          # power 0.80
SRM_P = 0.001               # split check fails below this
SRM_MIN_N = 100             # ... once at least this many people are randomised
RATE_WINDOW_DAYS = 28       # weeks-to-readable uses the last 4 weeks' pace

REGISTRY_FILE = "experiments.json"
REQUIRED = ("exp_id", "sheet_id", "name", "gym", "arms")

# the columns the cron adds to the right of Chris's own (A-Q), in this order
V2_HEADERS = [
    "Primary outcome", "Baseline / MWE", "Needed per arm", "All people A / B",
    "Lift (B - A), 95% CI", "Opened first A / B", "Split check", "Health",
    "Progress", "Weeks to readable", "Statistical result",
]
V2_NOTES = {
    "Primary outcome": "The one thing this test is judged on, per person, counted only "
                       "when it happened inside the window after that person's email.",
    "Baseline / MWE": "Baseline = the rate we expect without a change. MWE = the smallest "
                      "improvement worth acting on, in percentage points. Together they "
                      "set how many people each arm needs.",
    "Needed per arm": "People per arm before the test can be read (95% confidence, 80% "
                      "power). Until both arms reach it the result says 'Not readable yet'.",
    "All people A / B": "Everyone randomised into each arm so far, including people whose "
                        "window has not closed yet. Sample Size A/B counts only the ones "
                        "whose window has closed.",
    "Lift (B - A), 95% CI": "B's rate minus A's, in percentage points, with the range the "
                            "true difference most likely sits in. A range that crosses 0 "
                            "means we cannot tell the arms apart yet.",
    "Opened first A / B": "Of the people counted as a success, how many had opened that "
                          "email before they acted. Not the rule for credit, just context.",
    "Split check": "Were people split evenly between A and B? UNEVEN means something is "
                   "steering people into one arm and the result cannot be trusted.",
    "Health": "Collecting = still gathering people. Pass = readable and the split is fine. "
              "Fail = the split check failed.",
    "Progress": "Mature people in the smaller arm out of the number needed.",
    "Weeks to readable": "At the last 4 weeks' pace, roughly how long until both arms are "
                         "big enough. 'no recent sends' = the test is not gathering people.",
    "Statistical result": "A wins / B wins = a real difference at 95% confidence. "
                          "Equivalent = the arms are within the MWE of each other. "
                          "Inconclusive = readable but no clear answer. Not readable yet = "
                          "wait.",
}


def _d(s: str) -> date | None:
    try:
        return date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def load_registry(client_dir: Path) -> tuple[list[dict], list[str]]:
    """(entries, notes). A missing file = no experiments (the caller keeps the
    old dropdown-text auto-fill). A broken entry is skipped with a note that
    rides into the report email; nothing here raises."""
    path = Path(client_dir) / REGISTRY_FILE
    if not path.exists():
        return [], []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [], [f"experiments.json unreadable ({exc}); v2 skipped"]
    items = raw.get("experiments", raw) if isinstance(raw, dict) else raw
    out, notes = [], []
    for i, e in enumerate(items or []):
        if not isinstance(e, dict):
            notes.append(f"experiments.json entry {i}: not an object, skipped")
            continue
        missing = [k for k in REQUIRED if not e.get(k)]
        if missing:
            notes.append(f"experiments.json entry {i} ({e.get('exp_id') or '?'}): "
                         f"missing {', '.join(missing)}, skipped")
            continue
        arms = e["arms"]
        bucket = (e.get("bucket") or "").strip() if isinstance(e.get("bucket"), str) else ""
        if bucket:
            # a bucket test: arms may be empty (A = nothing sent), but the
            # population must say who is in the test
            pop = e.get("population")
            tags = pop.get("tags") if isinstance(pop, dict) else None
            if not isinstance(arms, dict) or not tags or not isinstance(tags, (list, str)):
                notes.append(f"{e['exp_id']}: a bucket test needs arms {{'A': [...], 'B': [...]}} "
                             f"and population.tags, skipped")
                continue
        elif not isinstance(arms, dict) or not arms.get("A") or not arms.get("B"):
            notes.append(f"{e['exp_id']}: arms must be {{'A': [...], 'B': [...]}}, skipped")
            continue
        e = dict(e)
        e["arms"] = {k: ([v] if isinstance(v, str) else list(v or [])) for k, v in arms.items()}
        e["arms"].setdefault("A", [])
        e["arms"].setdefault("B", [])
        if bucket:
            pop = dict(e["population"])
            tags = pop.get("tags")
            pop["tags"] = [tags] if isinstance(tags, str) else list(tags)
            pop["since"] = str(pop.get("since") or "")[:10]
            e["population"] = pop
            e["bucket"] = bucket
        e.setdefault("primary_outcome", DEFAULT_OUTCOME)
        if e["primary_outcome"] not in OUTCOMES:
            notes.append(f"{e['exp_id']}: unknown primary_outcome "
                         f"'{e['primary_outcome']}', using {DEFAULT_OUTCOME}")
            e["primary_outcome"] = DEFAULT_OUTCOME
        e.setdefault("mwe", DEFAULT_MWE)
        e.setdefault("categories", [])
        e.setdefault("active", True)
        out.append(e)
    return out, notes


# --- statistics ---------------------------------------------------------------

def _p_two_sided(z: float) -> float:
    return math.erfc(abs(z) / math.sqrt(2))


def needed_per_arm(baseline: float, mwe: float) -> int:
    """People per arm to detect baseline -> baseline + mwe (two-sided 0.05,
    power 0.8), the textbook two-proportion formula."""
    p1 = min(max(float(baseline), 1e-6), 1 - 1e-6)
    p2 = min(max(p1 + float(mwe), 1e-6), 1 - 1e-6)
    if abs(p2 - p1) < 1e-9:
        return 0
    pbar = (p1 + p2) / 2
    num = (Z_ALPHA * math.sqrt(2 * pbar * (1 - pbar))
           + Z_POWER * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2
    return int(math.ceil(num / (p2 - p1) ** 2))


def two_prop(sa: int, na: int, sb: int, nb: int) -> dict | None:
    """Difference B - A with a 95% Wald CI and a pooled two-proportion z-test.
    None when either arm is empty."""
    if na <= 0 or nb <= 0:
        return None
    pa, pb = sa / na, sb / nb
    diff = pb - pa
    se = math.sqrt(pa * (1 - pa) / na + pb * (1 - pb) / nb)
    lo, hi = diff - Z_ALPHA * se, diff + Z_ALPHA * se
    pool = (sa + sb) / (na + nb)
    se0 = math.sqrt(pool * (1 - pool) * (1 / na + 1 / nb))
    z = diff / se0 if se0 > 0 else 0.0
    return {"pa": pa, "pb": pb, "diff": diff, "lo": lo, "hi": hi,
            "z": z, "p": _p_two_sided(z) if se0 > 0 else 1.0}


def srm_p(na: int, nb: int) -> float:
    """Chi-square (1 df) that a 50/50 split produced these arm sizes."""
    n = na + nb
    if n == 0:
        return 1.0
    chi = (na - nb) ** 2 / n
    return math.erfc(math.sqrt(chi / 2))


# --- people and outcomes ------------------------------------------------------

def bucket_of(email: str, test: str) -> str:
    """The person's arm in a bucket test: sha256 of "email:test", even = A.
    MUST stay equal to engine._ab_variant (the sender's rule); the verify
    suite checks the two agree."""
    h = hashlib.sha256(
        f"{(email or '').strip().lower()}:{test}".encode()).hexdigest()
    return "A" if int(h, 16) % 2 == 0 else "B"


def _person_table_bucket(exp: dict, records: list[dict]) -> tuple[dict, list[str]]:
    """Bucket test: everyone with a population send on/after `since` is in,
    arm = bucket_of(email, bucket). Arm-tag sends (the reminder) are added to
    their person's list; one to a person outside the population is ignored,
    one to an arm-A person is counted in a note (it should never happen)."""
    pop = exp.get("population") or {}
    pop_tags = set(pop.get("tags") or [])
    since = (pop.get("since") or "")[:10]
    test = exp["bucket"]
    arms: dict = {"A": {}, "B": {}}
    for r in records:
        tag, vt = r.get("tag"), r.get("var_tag")
        if (tag in pop_tags or vt in pop_tags) and (not since or r["sent"] >= since):
            arms[bucket_of(r["email"], test)].setdefault(r["email"], []).append(r)
    arm_tags = {k: set(v) for k, v in exp["arms"].items()}
    stray = outside = 0
    for r in records:
        tag, vt = r.get("tag"), r.get("var_tag")
        for arm, ts in arm_tags.items():
            if not ts or (tag not in ts and vt not in ts):
                continue
            email = r["email"]
            mine = bucket_of(email, test)
            if email not in arms[mine]:
                outside += 1  # e.g. surveyed before `since`; the sender's floor should stop this
                continue
            if mine != arm:
                stray += 1
            if r not in arms[mine][email]:
                arms[mine][email].append(r)
    notes = []
    if stray:
        notes.append(f"{exp['exp_id']}: {stray} arm-tag send(s) went to a person the "
                     f"hash puts in the other arm")
    if outside:
        notes.append(f"{exp['exp_id']}: {outside} arm-tag send(s) went to people outside "
                     f"the population (sender floor and population.since disagree?)")
    return arms, notes


def person_table(exp: dict, records: list[dict]) -> tuple[dict, list[str]]:
    """{'A': {email: [record, ...]}, 'B': {...}} from the send records; a
    record joins an arm when its tag or its A/B variant tag is listed there.
    A person seen in both arms is dropped from both (noted). A bucket test
    (see the module docstring) assigns people by hash instead."""
    if exp.get("bucket"):
        return _person_table_bucket(exp, records)
    arms = {"A": {}, "B": {}}
    tags = {k: set(v) for k, v in exp["arms"].items()}
    for r in records:
        for arm, ts in tags.items():
            if r.get("tag") in ts or r.get("var_tag") in ts:
                arms[arm].setdefault(r["email"], []).append(r)
    both = set(arms["A"]) & set(arms["B"])
    for e in both:
        arms["A"].pop(e, None)
        arms["B"].pop(e, None)
    notes = ([f"{exp['exp_id']}: {len(both)} person(s) seen in both arms, dropped"]
             if both else [])
    return arms, notes


def _within(opened_at: str, sent: str, window: int) -> bool:
    oa, sd = _d(opened_at), _d(sent)
    if oa is None or sd is None:
        return True  # undated open: opens land within days of the send
    return (oa - sd).days <= window


# outcomes whose event date collect() writes onto the record as `<base>_at`
DATED_BASES = ("responded", "converted")


def person_success(recs: list[dict], outcome: str,
                   clock: str | None = None) -> tuple[bool, bool | None]:
    """(success, opened_first). opened_first is None for the open/click
    outcomes (there the open IS the outcome).

    clock (bucket tests): the date the outcome window runs from for this
    person (their first population send). A dated outcome then needs its
    event inside `window` days of the clock, not of the crediting send, so a
    response after a day-2 reminder is still judged against the first email.
    Undated outcomes keep the credit's own window from the crediting send."""
    _label, base, window = OUTCOMES[outcome]
    if base == "opened":
        return any(r.get("opened") and _within(r.get("opened_at", ""), r["sent"], window)
                   for r in recs), None
    if base == "clicked":
        return any(bool(r.get("clicked")) for r in recs), None
    hit = [r for r in recs if (r.get("credits") or {}).get(base)]
    if clock and base in DATED_BASES:
        hit = [r for r in hit if _inside(r.get(base + "_at", ""), clock, window)]
    elif window < CREDIT_WINDOWS.get(base, window):
        # a yardstick tighter than the credit window needs the event date on
        # the record (`<base>_at`, written by collect()); no date = not inside
        hit = [r for r in hit if _inside(r.get(base + "_at", ""), r["sent"], window)]
    if not hit:
        return False, False
    return True, any((r.get("credits") or {}).get(base + "_opened") for r in hit)


def _inside(event_at: str, sent: str, window: int) -> bool:
    ea, sd = _d(event_at), _d(sent)
    return ea is not None and sd is not None and 0 <= (ea - sd).days <= window


def _first_sent(recs: list[dict]) -> str:
    return min(r["sent"] for r in recs)


def evaluate(exp: dict, records: list[dict], today: date) -> dict:
    """Everything the sheet row needs for one experiment."""
    outcome = exp.get("primary_outcome", DEFAULT_OUTCOME)
    label, base, window = OUTCOMES[outcome]
    arms, notes = person_table(exp, records)
    baseline = exp.get("baseline")
    mwe = float(exp.get("mwe", DEFAULT_MWE))
    bucket = bool(exp.get("bucket"))
    stats: dict = {}
    for arm in ("A", "B"):
        people = arms[arm]
        mature = {e: rs for e, rs in people.items()
                  if (today - (_d(_first_sent(rs)) or today)).days >= window}
        succ = opened_first = 0
        for rs in mature.values():
            ok, of = person_success(rs, outcome,
                                    clock=_first_sent(rs) if bucket else None)
            succ += ok
            opened_first += bool(ok and of)
        recent = sum(1 for rs in people.values()
                     if (_d(_first_sent(rs)) or date.min) >= today - timedelta(days=RATE_WINDOW_DAYS))
        stats[arm] = {"n_all": len(people), "n_mature": len(mature), "successes": succ,
                      "opened_first": opened_first,
                      "rate": (succ / len(mature)) if mature else None,
                      "recent": recent}
    a, b = stats["A"], stats["B"]
    # baseline defaults to the observed pooled mature rate (or the outcome's
    # typical rate) so a registry entry without one still gets a target
    if baseline is None:
        pooled_n = a["n_mature"] + b["n_mature"]
        baseline = ((a["successes"] + b["successes"]) / pooled_n) if pooled_n else 0.1
        baseline = max(baseline, 0.01)
    needed = needed_per_arm(baseline, mwe)
    min_mature = min(a["n_mature"], b["n_mature"])
    readable = needed > 0 and min_mature >= needed
    tp = two_prop(a["successes"], a["n_mature"], b["successes"], b["n_mature"])
    n_all = a["n_all"] + b["n_all"]
    split_p = srm_p(a["n_all"], b["n_all"])
    split_bad = n_all >= SRM_MIN_N and split_p < SRM_P
    if split_bad:
        health, result = "Fail: uneven split", "Invalid (uneven split)"
    elif not readable:
        health, result = "Collecting", "Not readable yet"
    else:
        health = "Pass"
        if tp and tp["p"] < 0.05:
            result = "B wins" if tp["diff"] > 0 else "A wins"
        elif tp and -mwe <= tp["lo"] and tp["hi"] <= mwe:
            result = "Equivalent"
        else:
            result = "Inconclusive"
    pct = int(round(100 * min_mature / needed)) if needed else 100
    progress = f"{min_mature:,} / {needed:,} per arm ({pct}%)"
    pace = min(a["recent"], b["recent"]) / (RATE_WINDOW_DAYS / 7)  # people per arm per week
    if readable:
        weeks = "readable now"
    elif pace <= 0:
        weeks = "no recent sends"
    else:
        weeks = str(int(math.ceil((needed - min_mature) / pace + window / 7)))
    if split_bad:
        split = f"UNEVEN (p={split_p:.4f})"
    elif n_all < SRM_MIN_N:
        split = f"too early ({n_all} people)"
    else:
        split = f"OK (p={split_p:.2f})"
    if tp:
        lift = (f"{100 * tp['diff']:+.1f} pts ({100 * tp['lo']:+.1f} to "
                f"{100 * tp['hi']:+.1f})")
    else:
        lift = ""
    of = "n/a" if base in ("opened", "clicked") else f"{a['opened_first']} / {b['opened_first']}"
    cells = {
        "Gym": exp["gym"],
        "Audience": exp.get("audience", ""),
        "Experiment Type": exp.get("type", ""),
        "Category": ", ".join(exp.get("categories") or []),
        "Variant A": exp.get("variant_a", ""),
        "Variant B": exp.get("variant_b", ""),
        "Success Metric": label,
        "Sample Size A": a["n_mature"],
        "Sample Size B": b["n_mature"],
        "Result A": round(a["rate"], 4) if a["rate"] is not None else "",
        "Result B": round(b["rate"], 4) if b["rate"] is not None else "",
        "Send It Test": exp["name"],
        "Primary outcome": f"{label} ({window}-day window)",
        "Baseline / MWE": f"{100 * float(baseline):.0f}% / +{100 * mwe:.0f} pts",
        "Needed per arm": needed,
        "All people A / B": f"{a['n_all']} / {b['n_all']}",
        "Lift (B - A), 95% CI": lift,
        "Opened first A / B": of,
        "Split check": split,
        "Health": health,
        "Progress": progress,
        "Weeks to readable": weeks,
        "Statistical result": result,
    }
    # drop descriptive cells the registry did not set, so Chris's own text stays
    for k in ("Audience", "Experiment Type", "Variant A", "Variant B"):
        if not cells[k]:
            cells.pop(k)
    if not exp.get("categories"):
        cells.pop("Category")
    return {"exp": exp, "outcome": outcome, "arms": stats, "needed": needed,
            "readable": readable, "test": tp, "split_p": split_p, "health": health,
            "result": result, "cells": cells, "notes": notes}
