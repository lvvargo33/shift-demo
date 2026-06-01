"""Eligibility engine: dataset + trigger catalog -> today's nudge queue + data.json.

This is the 'brain' the SPEC section-13 pivot moved out of the Google Sheet and
into the owned tool. Section 14 generalizes it from one trigger to the whole
message catalog (FTV_PILOT_SPEC.md section 14), applying the precedence rules in
section 14.7. It decides WHO gets WHICH tag WHEN. It does not talk to Mailchimp
(cli.py does, gated by mode) and does not send (a Journey does).

Precedence per run (section 14.7):
  1. Safety flag wins -> MANUAL, pause all auto-email for that person. (survey
     data, S2; structural hook here.)
  2. Converted -> stop. Member or bought a conversion_sku => no message.
  3. One message per person per run = highest-priority match (config order).
  4. Once-only / cooldown via the local outreach log. (wired in S3; hook here.)
  5. Unsubscribed / cleaned -> skip. (Mailchimp read, S3; hook here.)
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date

from . import ingest, templates
from .config import ClientConfig
from .ingest import Climber, Dataset
from .triggers import Trigger


def _active(client: ClientConfig) -> list[Trigger]:
    return [t for t in client.triggers if t.active]


@dataclass
class QueueItem:
    climber_id: str
    name: str
    email: str
    trigger_name: str
    tag: str
    template_id: str
    anchor: str
    anchor_date: str
    days_since: int
    visit_count: int
    blocker: str | None      # survey Q3 answer that matched (None until S2)
    survey_answered: bool = False    # has a matched survey response (S2)
    intent: str | None = None        # survey Q2 -> intent bucket (S2)
    safety_flag: bool = False        # survey Q4 tripped a safety keyword (S2)
    subject: str = ""
    body_preview: str = ""
    # Back-compat fields the T3 dashboard still reads (rebuilt in S4):
    trial_date: str = ""
    days_since_trial: int = 0
    order: int = 0           # config priority index (internal sort key)
    priority: int = 0        # 1 = act first, filled after the final sort
    # Per-person history for the Detail view:
    visit_dates: list = field(default_factory=list)
    daypass_dates: list = field(default_factory=list)
    first_visit: str = ""
    last_visit: str = ""
    is_member: bool = False
    is_converted: bool = False
    conversion_date: str = ""


# --- anchor resolution -------------------------------------------------------

def _anchor_date(c: Climber, anchor: str) -> str | None:
    return {
        "first_visit": c.first_visit_date,
        "trial_purchase": c.trial_date,
        "survey_sent": c.survey_sent_date,
        "last_visit": c.last_visit_date,
        "last_daypass": c.last_daypass_date,
    }.get(anchor)


# --- requirement evaluation --------------------------------------------------

def _requires_ok(c: Climber, req: dict, asof: date, client: ClientConfig) -> bool:
    """Every requirement must hold. Unknown keys FAIL CLOSED (never over-message)."""
    for key, val in req.items():
        if key == "no_return":
            if val and c.visit_count > 1:
                return False
        elif key == "max_visits":
            if c.visit_count > int(val):
                return False
        elif key == "min_visits":
            if c.visit_count < int(val):
                return False
        elif key == "daypass_repeat":
            if val:
                dr = client.daypass_repeat
                got = c.daypass_count_within(asof, int(dr.get("window_days", 30)))
                if got < int(dr.get("min_count", 2)):
                    return False
        elif key == "survey_blocker":
            if c.survey_blocker != val:
                return False
        elif key == "survey_intent":
            if c.survey_intent != val:
                return False
        elif key == "not_surveyed":
            if val and c.survey_answered:
                return False
        else:
            return False
    return True


def _match(c: Climber, t: Trigger, asof: date,
           client: ClientConfig) -> tuple[int, str] | None:
    """Return (days_since, anchor_date) if c qualifies for trigger t, else None."""
    ad = _anchor_date(c, t.anchor)
    if not ad:
        return None
    days = ingest.days_between(ad, asof)
    if days < t.days_since_min or days > t.days_since_max:
        return None
    if not _requires_ok(c, t.requires, asof, client):
        return None
    return days, ad


# --- suppression hooks (S3: outreach log + Mailchimp status) -----------------

def _suppressed(c: Climber, t: Trigger, asof: date,
                log, unsubscribed: set | None) -> str | None:
    """Rules 4 + 5 (section 14.7). Returns a reason string if this trigger must
    be suppressed for this climber, else None.

      rule 5: email is unsubscribed/cleaned in Mailchimp     -> "unsubscribed"
      rule 4: once_only trigger already sent (ever)          -> "already_sent"
              cooldown trigger sent within cooldown_days      -> "cooldown"

    Both inputs are optional: with no log and no status set (the S1/S2 default)
    nothing is suppressed, so older callers keep their exact behavior."""
    email = (c.email or "").strip().lower()
    if unsubscribed and email and email in unsubscribed:
        return "unsubscribed"
    if log is not None and email:
        dates = log.sent_dates(email, t.tag)
        if dates:
            if t.once_only:
                return "already_sent"
            cd = int(t.cooldown_days or 0)
            if cd > 0 and ingest.days_between(max(dates), asof) < cd:
                return "cooldown"
    return None


# --- queue construction ------------------------------------------------------

def _make_item(c: Climber, t: Trigger, days: int, anchor_date: str,
               order: int, client: ClientConfig) -> QueueItem:
    fn = ingest.first_name(c.name)
    subject, body = templates.render(client.templates, t.template_id, fn)
    return QueueItem(
        climber_id=c.climber_id, name=c.name, email=c.email,
        trigger_name=t.name, tag=t.tag, template_id=t.template_id,
        anchor=t.anchor, anchor_date=anchor_date, days_since=days,
        visit_count=c.visit_count, blocker=c.survey_blocker,
        survey_answered=c.survey_answered, intent=c.survey_intent,
        safety_flag=c.safety_flag,
        subject=subject, body_preview=body[:200],
        trial_date=c.trial_date or anchor_date, days_since_trial=days,
        order=order,
        visit_dates=sorted(c.visit_days),
        daypass_dates=list(c.daypass_dates),
        first_visit=c.first_visit_date or "",
        last_visit=c.last_visit_date or "",
        is_member=c.is_member, is_converted=c.is_converted,
        conversion_date=c.conversion_date or "",
    )


def _manual_item(c: Climber, order: int) -> QueueItem:
    """Safety flag -> manager call, no automated email. (S2 fills safety_flag.)"""
    ad = c.last_visit_date or c.trial_date or ""
    return QueueItem(
        climber_id=c.climber_id, name=c.name, email=c.email,
        trigger_name="MANUAL", tag="MANUAL", template_id="",
        anchor="manual", anchor_date=ad, days_since=0,
        visit_count=c.visit_count, blocker=c.survey_blocker,
        survey_answered=c.survey_answered, intent=c.survey_intent,
        safety_flag=c.safety_flag,
        subject="Manager call — safety concern flagged",
        body_preview=(c.survey_intent or ""), trial_date=c.trial_date or ad,
        days_since_trial=0, order=order,
    )


def build_queue(ds: Dataset, client: ClientConfig, asof: date,
                log=None, unsubscribed: set | None = None,
                suppressed_out: list | None = None) -> list[QueueItem]:
    """Build today's queue applying all five precedence rules.

    log / unsubscribed power rules 4 + 5; both default to off so the S1/S2
    behavior is byte-identical when they aren't passed. When suppressed_out is a
    list, every (climber, trigger, reason) we dropped for rule 4/5 is appended to
    it (so the caller can report "would have sent X but it's unsubscribed /
    already sent")."""
    active = _active(client)
    order_of = {t.name: i for i, t in enumerate(active)}
    items: list[QueueItem] = []

    for c in ds.climbers.values():
        # rule 1: safety MANUAL wins, pauses everything else.
        if c.safety_flag:
            items.append(_manual_item(c, order=-1))
            continue
        # rule 2: converted -> stop.
        if c.is_converted:
            continue
        # gather every trigger this person matches.
        matches: list[tuple[int, Trigger, int, str]] = []
        for t in active:
            m = _match(c, t, asof, client)
            if m is None:
                continue
            reason = _suppressed(c, t, asof, log, unsubscribed)  # rules 4 + 5 (S3)
            if reason:
                if suppressed_out is not None:
                    suppressed_out.append((c, t, reason))
                continue
            days, ad = m
            matches.append((order_of[t.name], t, days, ad))
        if not matches:
            continue
        # rule 3: one message per person -> highest priority (lowest config
        # index); tie-break oldest-in-window first.
        matches.sort(key=lambda m: (m[0], -m[2]))
        idx, t, days, ad = matches[0]
        items.append(_make_item(c, t, days, ad, idx, client))

    # Display order: priority group, then oldest-in-window, then name.
    items.sort(key=lambda i: (i.order, -i.days_since, i.name.lower()))
    for n, it in enumerate(items, 1):
        it.priority = n
    return items


# --- as-of, summary, payload, writers ---------------------------------------

def resolve_asof(ds: Dataset, override: str | None) -> date:
    """As-of date for the day-windows. Defaults to the export's latest
    transaction date, the honest freshness anchor for the trial cohort
    (and ~= today in daily production)."""
    if override:
        return date.fromisoformat(override)
    if ds.tx_max_date:
        return date.fromisoformat(ds.tx_max_date)
    raise RuntimeError("No transaction dates found; pass --as-of explicitly.")


def summarize(ds: Dataset, queue: list[QueueItem], client: ClientConfig) -> dict:
    one_and_done = sum(
        1 for c in ds.climbers.values()
        if c.trial_date and c.visit_count <= 1 and not c.is_converted
    )
    return {
        "trial_buyers": ds.trial_buyer_count,
        "one_and_done_not_member": one_and_done,
        "eligible_now": len(queue),
        "by_trigger": {
            t.name: sum(1 for q in queue if q.trigger_name == t.name)
            for t in _active(client)
        },
    }


def build_metrics(ds: Dataset, queue: list[QueueItem], client: ClientConfig,
                  asof: date) -> dict:
    """Real headline numbers for the Dashboard tiles. No industry benchmarks
    (per the SHIFT analysis stance) and nothing faked: every figure is a count
    off the Beta export."""
    climbers = ds.climbers.values()
    first_timers = sum(1 for c in climbers if c.visit_count >= 1)
    new_7d = sum(
        1 for c in climbers
        if c.first_visit_date and 0 <= ingest.days_between(c.first_visit_date, asof) <= 7
    )
    members = sum(1 for c in climbers if c.is_member)
    converted = sum(1 for c in climbers if c.is_converted)
    dr = client.daypass_repeat
    daypass_regulars = sum(
        1 for c in climbers
        if not c.is_converted
        and c.daypass_count_within(asof, int(dr.get("window_days", 30)))
        >= int(dr.get("min_count", 2))
    )
    one_and_done = sum(
        1 for c in climbers
        if c.trial_date and c.visit_count <= 1 and not c.is_converted
    )
    return {
        "first_timers": first_timers,
        "new_visitors_7d": new_7d,
        "trial_buyers": ds.trial_buyer_count,
        "one_and_done": one_and_done,
        "members": members,
        "converted": converted,
        "daypass_regulars": daypass_regulars,
        "eligible_now": len(queue),
    }


def build_funnel(ds: Dataset) -> list[dict]:
    """Trial-cohort funnel: the FTV->member story this project is about, told
    only with real counts. Stages are nested subsets of trial buyers so the
    bar widths are honest."""
    trials = [c for c in ds.climbers.values() if c.trial_date]
    n = len(trials)
    visited = sum(1 for c in trials if c.visit_count >= 1)
    returned = sum(1 for c in trials if c.visit_count >= 2)
    converted = sum(1 for c in trials if c.is_converted)

    def pct(x: int) -> float:
        return round(100 * x / n, 1) if n else 0.0

    return [
        {"label": "Bought a 2-week trial", "count": n, "pct": 100.0,
         "note": "The cohort"},
        {"label": "Checked in at least once", "count": visited, "pct": pct(visited),
         "note": "Showed up after buying"},
        {"label": "Came back (2+ visits)", "count": returned, "pct": pct(returned),
         "note": "The habit starts here"},
        {"label": "Became a member", "count": converted, "pct": pct(converted),
         "note": "Bought a recurring/prepaid plan or in Memberships"},
    ]


def build_config_view(client: ClientConfig) -> dict:
    """Read-only snapshot of the live config for the Settings view. Grounds the
    'everything is tunable per client, no code' story."""
    return {
        "client_name": client.client_name,
        "slug": client.slug,
        "tiers": {
            "trial_skus": client.trial_skus,
            "low_commitment_skus": client.low_commitment_skus,
            "conversion_skus": client.conversion_skus,
            "daypass_repeat": client.daypass_repeat,
        },
        "data_sources": {
            "transactions_csv": client.transactions_csv.name,
            "sessions_csv": client.sessions_csv.name,
            "memberships_csv": client.memberships_csv.name,
        },
        "triggers": [
            {"name": t.name, "tag": t.tag, "template_id": t.template_id,
             "anchor": t.anchor, "window_days": [t.days_since_min, t.days_since_max],
             "requires": t.requires, "once_only": t.once_only,
             "cooldown_days": t.cooldown_days, "active": t.active,
             "description": t.description}
            for t in client.triggers
        ],
        "safety": {
            "live_enabled": client.live_enabled,
            "locks": [
                "Refuses --live without --i-have-shift-signoff",
                "Refuses --live unless live_enabled is true in client.json",
                "Interactive type-the-client-name confirm at live tag time (S5)",
            ],
        },
    }


def _max_visits_compat(t: Trigger) -> int | None:
    """Back-compat for the T3 dashboard's active-nudge card (rebuilt in S4)."""
    if "max_visits" in t.requires:
        return int(t.requires["max_visits"])
    if t.requires.get("no_return"):
        return 1
    return None


def summarize_suppression(suppressed_out: list | None) -> dict:
    """Counts + the people for the rule 4/5 drops: matched a trigger but withheld.
    Honest visibility into who we did NOT message and why (shown on the dashboard
    so a withheld climber reads as 'already sent / unsubscribed', not 'queued')."""
    by_reason: dict = {}
    by_tag: dict = {}
    withheld: list = []
    if suppressed_out:
        for c, t, reason in suppressed_out:
            by_reason[reason] = by_reason.get(reason, 0) + 1
            by_tag[t.tag] = by_tag.get(t.tag, 0) + 1
            withheld.append({
                "climber_id": c.climber_id, "name": c.name, "email": c.email,
                "trigger_name": t.name, "tag": t.tag, "reason": reason,
            })
    return {
        "total": len(suppressed_out or []),
        "by_reason": by_reason,   # unsubscribed / already_sent / cooldown
        "by_tag": by_tag,
        "withheld": withheld,
    }


def _survey_block(client: ClientConfig, survey_result) -> dict:
    """The survey slice of the payload. Aggregates feed Insights; the per-response
    list feeds the Inbox two-pane. Empty (responses: []) whenever the survey loop
    is off, so the dashboard's Inbox/Insights stay honestly dark until real data."""
    responses = getattr(survey_result, "responses", []) or []
    return {
        "enabled": bool(client.survey.get("enabled")),
        "matched": getattr(survey_result, "matched", 0),
        "unmatched": getattr(survey_result, "unmatched", 0),
        "safety": getattr(survey_result, "safety_count", 0),
        "by_blocker": getattr(survey_result, "by_blocker", {}) or {},
        "responses": [
            {
                "email": r.email, "name": r.name, "climber_id": r.climber_id,
                "answered_at": r.answered_at, "matched": r.matched,
                "q1_overall": r.q1_overall, "q2_intent": r.q2_intent,
                "q3_blocker_raw": r.q3_blocker_raw, "q4_text": r.q4_text,
                "blocker": r.blocker, "intent": r.intent, "safety": r.safety,
            }
            for r in responses
        ],
    }


def build_payload(ds: Dataset, queue: list[QueueItem], client: ClientConfig,
                  asof: date, mode: str, generated_at: str,
                  survey_result=None, suppressed_out: list | None = None) -> dict:
    survey_block = _survey_block(client, survey_result)
    return {
        "client": client.client_name,
        "generated_at": generated_at,
        "mode": mode,
        "live_enabled": client.live_enabled,
        "as_of": asof.isoformat(),
        "data_window": {
            "transactions_max": ds.tx_max_date,
            "sessions_max": ds.sessions_max_date,
        },
        "summary": summarize(ds, queue, client),
        "suppression": summarize_suppression(suppressed_out),
        "survey": survey_block,
        "metrics": build_metrics(ds, queue, client, asof),
        "funnel": build_funnel(ds),
        "config": build_config_view(client),
        "triggers": [
            {"name": t.name, "tag": t.tag, "template_id": t.template_id,
             "anchor": t.anchor, "window_days": [t.days_since_min, t.days_since_max],
             "requires": t.requires, "max_visits": _max_visits_compat(t),
             "description": t.description}
            for t in _active(client)
        ],
        "queue": [asdict(q) for q in queue],
    }


def write_data_json(payload: dict, client: ClientConfig) -> str:
    client.out_dir.mkdir(parents=True, exist_ok=True)
    client.data_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return str(client.data_json)


def write_dashboard(payload: dict, client: ClientConfig) -> str:
    """Inject the run's data into the Send It skin -> one self-contained HTML.

    No fetch, no server: the JSON lives in a <script> tag. '<' is escaped to
    \\u003c (valid inside a JSON string) so an email body can never close the
    script tag early.
    """
    from .config import DASHBOARD_TEMPLATE
    template = DASHBOARD_TEMPLATE.read_text(encoding="utf-8")
    data_str = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    html = template.replace("__DATA_JSON__", data_str)
    client.out_dir.mkdir(parents=True, exist_ok=True)
    client.dashboard_html.write_text(html, encoding="utf-8")
    return str(client.dashboard_html)
