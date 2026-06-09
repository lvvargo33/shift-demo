"""Live send: turn the final queue into real-climber Mailchimp tag targets.

This is the real counterpart to testmode.py. Where --test remaps the queue onto
plus-addressed dummies, --live (and the send_nudges cron) tag the REAL climber
emails so a tag-triggered Mailchimp Journey sends the follow-up.

The queue handed in is ALREADY the final, safe list: engine.build_queue applied
all five precedence rules (safety MANUAL, converted-stop, one-per-person,
once-only/cooldown via the outreach log, unsubscribed) plus the shared-inbox
dedup. So this module only has to:
  - skip MANUAL safety items (no automated email, manager call instead) and any
    blank email (nothing to tag), and
  - emit one tag target per remaining climber, plus the matching outreach-log
    rows (mode='live') so a later run sees "already sent" and won't repeat.

Pure logic + string work: no network, no file writes (cli.py / send_nudges.py
own those, exactly like testmode).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from . import ingest


@dataclass
class SendTarget:
    climber_id: str
    name: str
    first_name: str
    email: str          # the REAL climber email we tag
    tag: str
    template_id: str
    trigger_name: str
    anchor_date: str = ""
    days_since: int = 0


def derive_sends(queue) -> list[SendTarget]:
    """One tag target per real climber in the queue, in priority order.

    Skips MANUAL safety items (they get a manager call, never an automated
    email) and any blank email. Everything else in the queue is already
    suppression-checked and deduped, so it is cleared to tag."""
    out: list[SendTarget] = []
    for q in queue:
        if q.trigger_name == "MANUAL":
            continue
        email = (q.email or "").strip()
        if not email:
            continue
        out.append(SendTarget(
            climber_id=q.climber_id,
            name=q.name,
            first_name=ingest.first_name(q.name),
            email=email,
            tag=q.tag,
            template_id=q.template_id,
            trigger_name=q.trigger_name,
            anchor_date=q.anchor_date,
            days_since=q.days_since,
        ))
    return out


def log_rows(sent: list[SendTarget], asof: date) -> list[dict]:
    """Outreach-log rows for the climbers we actually tagged (mode='live').
    Records the real email + tag so once-only/cooldown suppression works on the
    next run, and the dashboard's sent-log export has a row per send."""
    d = asof.isoformat()
    return [{
        "sent_date": d,
        "email": t.email,
        "climber_id": t.climber_id,
        "trigger_name": t.trigger_name,
        "tag": t.tag,
        "mode": "live",
    } for t in sent]
