"""Daily alarm: did Mailchimp really send what we logged as sent?

SHIFT's offer emails are sent by Mailchimp journeys. This tool only adds a
tag and writes a row to the outreach log; if the journey is paused, has an
entry filter, or was deleted, the tag lands, the row says "sent", and nobody
gets an email (a phantom send). That happened 2026-11-06 to 2026-11-19: the
survey follow-up journeys were paused on purpose, the triggers stayed on, and
two people were logged as sent a 'too expensive' email that never went out.
Nothing flagged it for 13 days.

The check: for every Mailchimp-path row logged in the last few days, read
that contact's Mailchimp activity and look for a journey email actually sent
on or after the logged date (the same rule the All Gyms sheet uses for
Delivered, engine._sent_campaign_ids). A row with no such email is reported.

Read-only on Mailchimp. Never raises into the send run: send_nudges.py calls
it inside a guard (CLAUDE.md rule 5: a failure prints a FAILED line).

SHIFT only (rule 4, a real per-gym difference): ABC sends every email itself
through Brevo, where a failed send is already caught at send time.
"""
from __future__ import annotations

from datetime import date, timedelta

from . import engine

# Yesterday back to this many days ago. A journey email goes out minutes
# after the tag, so yesterday's rows must already show. Each row is therefore
# re-checked for LOOKBACK_DAYS mornings: a missed alert repeats, it does not
# vanish after one report.
LOOKBACK_DAYS = 5


def _is_mailchimp_row(r: dict) -> bool:
    if (r.get("mode") or "").strip().lower() != "live":
        return False                                  # test rows tag dummies
    return (r.get("esp") or "").strip().lower() != "brevo"   # blank esp = Mailchimp


def _iso(day: str) -> bool:
    try:
        date.fromisoformat(day)
        return True
    except ValueError:
        return False


def due_rows(rows: list[dict], today: date, lookback: int = LOOKBACK_DAYS) -> list[dict]:
    """The logged Mailchimp sends old enough to judge and new enough to matter."""
    lo = (today - timedelta(days=lookback)).isoformat()
    hi = (today - timedelta(days=1)).isoformat()
    return [r for r in rows
            if _is_mailchimp_row(r) and lo <= (r.get("sent_date") or "")[:10] <= hi
            and _iso((r.get("sent_date") or "")[:10]) and (r.get("email") or "").strip()]


def _journey_sends(ev: list, journey_ids: set) -> list[date]:
    """Days Mailchimp sent this contact a journey email, oldest first."""
    days = []
    for a in ev or []:
        if a.get("activity_type") != "sent" or a.get("campaign_id") not in journey_ids:
            continue
        when = str(a.get("created_at_timestamp") or a.get("timestamp") or "")[:10]
        try:
            days.append(date.fromisoformat(when))
        except ValueError:
            continue
    return sorted(days)


def check(mc, rows: list[dict], today: date, lookback: int = LOOKBACK_DAYS) -> dict:
    """{"checked": n, "missing": [row...], "unread": [row...], "type_errors": n,
    "from": d, "to": d}.
    missing = logged as sent, no journey email in Mailchimp's record.
    unread  = that contact's activity could not be read (not judged).

    A row is proven by a journey email dated its logged day up to
    engine._SENT_MATCH_HORIZON_DAYS later (the All Gyms sheet's Delivered
    rule). Each email proves ONE row only: a person's rows are matched oldest
    first, so a real email on Tuesday cannot also cover a phantom row from
    Sunday or a second tag the same day (fresh-eyes finding 2026-11-19).

    Known limits. (1) The email is not tied to the row's TAG (Mailchimp's
    feed does not carry it): a different journey email to the same person
    inside the window, e.g. the gym's own welcome series, still counts as
    proof. (2) A journey that waits 3+ days before sending would read as a
    miss; none of ours do."""
    due = due_rows(rows, today, lookback)
    feeds: dict[str, list | None] = {}
    for r in due:
        e = r["email"].strip().lower()
        if e in feeds:
            continue
        try:
            feeds[e] = mc.member_activity(e)
        except Exception:  # noqa: BLE001 - one unreadable contact must not hide the rest
            feeds[e] = None
    cids = {a.get("campaign_id") for ev in feeds.values() for a in ev or []
            if a.get("campaign_id") and a.get("activity_type") == "sent"}
    journey_ids, type_errors = set(), 0
    for c in cids:
        try:
            if mc.campaign_type(c) == "automation-email":
                journey_ids.add(c)
        except Exception:  # noqa: BLE001 - one failed lookup must not kill the check
            # unknown type: count it as a journey email, so a Mailchimp blip
            # can make the check miss something but never raise a false alarm
            journey_ids.add(c)
            type_errors += 1
    horizon = timedelta(days=engine._SENT_MATCH_HORIZON_DAYS)
    missing, unread = [], []
    by_person: dict[str, list[dict]] = {}
    for r in due:
        by_person.setdefault(r["email"].strip().lower(), []).append(r)
    for e, prs in by_person.items():
        ev = feeds[e]
        if ev is None:
            unread.extend(prs)
            continue
        # each email goes to the NEWEST unproven row it can prove: a journey
        # fires minutes after its tag, so the closest row before it is the one
        open_rows = [(date.fromisoformat(r["sent_date"][:10]), i, r) for i, r in enumerate(prs)]
        for d in _journey_sends(ev, journey_ids):
            fits = [x for x in open_rows if x[0] <= d <= x[0] + horizon]
            if fits:
                open_rows.remove(max(fits, key=lambda x: (x[0], x[1])))
        missing.extend(r for _lo, _i, r in open_rows)
    return {"checked": len(due) - len(unread), "missing": missing, "unread": unread,
            "type_errors": type_errors,
            "from": (today - timedelta(days=lookback)).isoformat(),
            "to": (today - timedelta(days=1)).isoformat()}


def report_lines(res: dict) -> list[str]:
    """The lines for the send report. A problem line always carries FAILED."""
    span = f"{res['from']} to {res['to']}"
    out: list[str] = []
    miss, unread = res["missing"], res["unread"]
    if miss:
        tags = sorted({r.get("tag") or "?" for r in miss})
        out.append(f"  delivery check FAILED: {len(miss)} of {res['checked']} Mailchimp send(s) logged "
                   f"{span} NEVER WENT OUT (Mailchimp shows no journey email for them).")
        out.append(f"  Tags: {', '.join(tags)}. Likely cause: that journey is paused, has an entry "
                   f"filter, or the person already had the tag.")
        out.append("  ACTION: check the journey in Mailchimp, or turn the trigger off. These people "
                   "are logged as sent, so they are NOT retried. The same row shows here for "
                   f"{LOOKBACK_DAYS} mornings.")
        for r in sorted(miss, key=lambda x: (x.get("sent_date") or "", x.get("tag") or "")):
            out.append(f"    NOT SENT {(r.get('tag') or '?'):<32} {(r.get('sent_date') or '')[:10]}  {r.get('email')}")
    elif res["checked"] or not unread:
        out.append(f"  delivery check: {res['checked']} Mailchimp send(s) logged {span}, "
                   f"every one shows as sent in Mailchimp.")
    if unread and not res["checked"]:
        out.append(f"  delivery check FAILED to judge anything: Mailchimp activity did not load for any "
                   f"of the {len(unread)} send(s) logged {span}. Nothing is proven either way.")
    elif unread:
        out.append(f"  delivery check: {len(unread)} more send(s) could not be judged (that contact's "
                   f"Mailchimp activity did not load).")
    if res.get("type_errors"):
        out.append(f"  delivery check: {res['type_errors']} campaign lookup(s) did not load; those "
                   f"emails were counted as journey emails.")
    return out


def unproven(res: dict) -> bool:
    """True when the check ran but could judge nothing (subject line flag)."""
    return bool(res["unread"]) and not res["checked"]
