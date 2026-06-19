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
from datetime import date, timedelta
from types import SimpleNamespace

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
        elif key == "entry_category":
            # val is a list of reporting ftv_category codes (DAY_PASS, TRIAL_2WK,
            # ...). Match only climbers whose first-visit purchase is one of them.
            # Climbers with no qualifying purchase (ftv_category None = guests /
            # comps / youth-on-a-parent's-account) FAIL, so a paid-entry trigger
            # never messages the host's or parent's inbox.
            if c.ftv_category not in set(val):
                return False
        elif key == "first_visit_on_or_after":
            # Forward-only floor. Only message climbers whose FIRST visit is on or
            # after this date. Empty/blank value = no floor. Set it to the go-live
            # date at activation so NOTHING historical is ever sent: on day one the
            # queue is 0 and it fills naturally as new first-timers arrive.
            if val and (c.first_visit_date or "") < val:
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
                log, unsubscribed: set | None,
                group_siblings: list[str] | None = None) -> str | None:
    """Rules 4 + 5 (section 14.7). Returns a reason string if this trigger must
    be suppressed for this climber, else None.

      rule 5: email is unsubscribed/cleaned in Mailchimp     -> "unsubscribed"
      rule 4: once_only trigger already sent (ever)          -> "already_sent"
              cooldown trigger sent within cooldown_days      -> "cooldown"
      group:  any OTHER trigger in this trigger's campaign    -> "group_sent"
              group has already been sent to this email
              (so a climber gets exactly one message per group)

    All optional: with no log and no status set (the S1/S2 default) nothing is
    suppressed, so older callers keep their exact behavior."""
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
        # group cap: if any sibling trigger in the same campaign group has gone
        # out to this inbox, suppress this one (one message per group per person).
        for sib in (group_siblings or ()):
            if sib != t.tag and log.sent_dates(email, sib):
                return "group_sent"
    return None


# --- dedup by email (rule 6: one email per inbox) ----------------------------

def _dedup_by_email(items: list[QueueItem],
                    suppressed_out: list | None) -> list[QueueItem]:
    """Collapse the queue so each email address gets at most one nudge.

    Some climbers share one email (e.g. a family booked under one address), so
    without this they would each get their own follow-up and that single inbox
    would receive several. `items` is already in priority order, so we keep the
    FIRST (highest-priority) nudge per email and drop the rest, recording each
    drop in suppressed_out as 'shared_inbox' so it shows on the dashboard as
    withheld rather than vanishing.

    Never deduped: MANUAL safety items (they send no email and must always
    surface) and blank emails (nothing to collapse on)."""
    seen: dict[str, QueueItem] = {}
    kept: list[QueueItem] = []
    for it in items:
        email = (it.email or "").strip().lower()
        if it.trigger_name == "MANUAL" or not email:
            kept.append(it)
            continue
        if email in seen:
            if suppressed_out is not None:
                c_like = SimpleNamespace(climber_id=it.climber_id,
                                         name=it.name, email=it.email)
                t_like = SimpleNamespace(name=it.trigger_name, tag=it.tag)
                suppressed_out.append((c_like, t_like, "shared_inbox"))
            continue
        seen[email] = it
        kept.append(it)
    return kept


# --- queue construction ------------------------------------------------------

def _make_item(c: Climber, t: Trigger, days: int, anchor_date: str,
               order: int, client: ClientConfig) -> QueueItem:
    fn = ingest.first_name(c.name)
    subject, body = templates.render(client.templates, t.template_id, fn, client.links)
    return QueueItem(
        climber_id=c.climber_id, name=c.name, email=c.email,
        trigger_name=t.name, tag=t.tag, template_id=t.template_id,
        anchor=t.anchor, anchor_date=anchor_date, days_since=days,
        visit_count=c.visit_count, blocker=c.survey_blocker,
        survey_answered=c.survey_answered, intent=c.survey_intent,
        safety_flag=c.safety_flag,
        subject=subject, body_preview=body,
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
                suppressed_out: list | None = None,
                dedup_email: bool = False,
                own_email_guard: bool = False) -> list[QueueItem]:
    """Build today's queue applying all five precedence rules.

    log / unsubscribed power rules 4 + 5; both default to off so the S1/S2
    behavior is byte-identical when they aren't passed. When suppressed_out is a
    list, every (climber, trigger, reason) we dropped for rule 4/5 is appended to
    it (so the caller can report "would have sent X but it's unsubscribed /
    already sent").

    dedup_email (off by default, on in the real cli/live callers): collapse the
    queue to one nudge per email address so a shared (family) inbox isn't sent
    several. Dropped duplicates are recorded in suppressed_out as 'shared_inbox'.

    own_email_guard (off by default, on in the real callers): never message an
    email that belongs to more than one climber in the dataset. A guest often
    checks in under the host's email, and a family may share one inbox, so a
    shared address can't be safely tied to this one climber. Withheld items are
    recorded in suppressed_out as 'shared_email'."""
    active = _active(client)
    order_of = {t.name: i for i, t in enumerate(active)}
    # campaign-group membership: group name -> all tags in that group.
    group_tags: dict[str, list[str]] = {}
    for t in active:
        if t.group:
            group_tags.setdefault(t.group, []).append(t.tag)
    # own-email guard: emails used by more than one distinct climber.
    shared_emails: set[str] = set()
    if own_email_guard:
        counts: dict[str, int] = {}
        for c in ds.climbers.values():
            e = (c.email or "").strip().lower()
            if e:
                counts[e] = counts.get(e, 0) + 1
        shared_emails = {e for e, n in counts.items() if n > 1}
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
            siblings = group_tags.get(t.group) if t.group else None
            reason = _suppressed(c, t, asof, log, unsubscribed, siblings)  # rules 4 + 5 + group
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
        # own-email guard: a shared inbox can't be tied to this climber -> withhold.
        if own_email_guard and (c.email or "").strip().lower() in shared_emails:
            if suppressed_out is not None:
                suppressed_out.append((c, t, "shared_email"))
            continue
        items.append(_make_item(c, t, days, ad, idx, client))

    # Display order: priority group, then oldest-in-window, then name.
    items.sort(key=lambda i: (i.order, -i.days_since, i.name.lower()))
    # rule 6: one nudge per shared inbox (after the priority sort so the
    # highest-priority nudge is the one kept for that email).
    if dedup_email:
        items = _dedup_by_email(items, suppressed_out)
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


def build_reporting(ds: Dataset, client: ClientConfig, asof: date) -> dict:
    """Workbook-aligned reporting slice (Dashboard tiles / Funnel / Insights).

    Ships one compact record per FTV in the cohort so the dashboard can re-scope
    by period (all-time / year / month) entirely client-side. Definitions mirror
    SHIFT_Analysis/scripts/01_data_prep.py (see DASHBOARD_REVAMP_SPEC):
      - cohort  = climbers with an FTV-qualifying first purchase, NOT staff, on
                  or after reporting.post_opening_date (PRESALE founders excluded).
      - convert = reporting_converted (a real Memberships row).
      - period  = bucketed by FTV first-visit month.
    Returns enabled:false when no reporting config is present (older clients)."""
    rep = client.reporting or {}
    if not rep.get("ftv_qualifying_categories"):
        return {"enabled": False, "ftvs": []}
    opening = rep.get("post_opening_date")
    # Entry products to keep OUT of the reporting cohort entirely. Direct
    # memberships (climbers who bought a membership on their first visit) are
    # already members on arrival, so a first-visit follow-up never drove them;
    # crediting them would overstate what the tool can move. They are excluded
    # from the dashboard tiles / funnel / insights (the Recovery Queue is
    # unaffected; it skips members already). Config-driven, no code per client.
    excluded_cats = set(rep.get("exclude_from_cohort", []))
    ftvs = []
    for c in ds.climbers.values():
        fd = c.ftv_date
        if not fd or c.is_staff:
            continue
        if c.ftv_category in excluded_cats:
            continue
        if opening and fd < opening:
            continue
        dtc = None
        if c.reporting_converted and c.membership_created and c.membership_created >= fd:
            dtc = ingest.days_between(fd, date.fromisoformat(c.membership_created))
        ftvs.append({
            "m": fd[:7],                 # YYYY-MM cohort month
            "d": fd,                     # full FTV date (for rolling windows)
            "cat": c.ftv_category,       # entry product
            "nv": min(c.visit_count, 99),  # distinct check-in days (capped)
            "v1": c.visit_count >= 1,    # checked in at least once
            "ret": c.visit_count >= 2,   # came back (2+ visits)
            "conv": bool(c.reporting_converted),
            "dtc": dtc,                  # days from FTV to becoming a member (or null)
        })
    return {
        "enabled": True,
        "post_opening_date": opening,
        "as_of": asof.isoformat(),
        "entry_labels": rep.get("entry_product_labels", {}),
        "ftvs": ftvs,
    }


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


# The Inbox lists individual survey responses. To keep it navigable once the
# pilot has run for months, it only shows responses from the last N days; older
# ones drop off the list (the all-time aggregates above the list are unaffected).
INBOX_RESPONSE_WINDOW_DAYS = 14


def _within_inbox_window(answered_at: str) -> bool:
    """True if a response is recent enough to show in the Inbox list. Blank/odd
    dates are kept (shown), so we never silently hide a response we can't date."""
    d = (answered_at or "").strip()[:10]
    if len(d) != 10 or d[4] != "-":
        return True
    cutoff = (date.today() - timedelta(days=INBOX_RESPONSE_WINDOW_DAYS)).isoformat()
    return d >= cutoff


def _survey_block(client: ClientConfig, survey_result) -> dict:
    """The survey slice of the payload. Aggregates feed Insights; the per-response
    list feeds the Inbox two-pane. Empty (responses: []) whenever the survey loop
    is off, so the dashboard's Inbox/Insights stay honestly dark until real data.

    The per-response list is windowed to the last INBOX_RESPONSE_WINDOW_DAYS days
    so the Inbox stays readable over a long pilot; the aggregate counts remain
    all-time."""
    responses = getattr(survey_result, "responses", []) or []
    return {
        "enabled": bool(client.survey.get("enabled")),
        "matched": getattr(survey_result, "matched", 0),
        "unmatched": getattr(survey_result, "unmatched", 0),
        "safety": getattr(survey_result, "safety_count", 0),
        "by_blocker": getattr(survey_result, "by_blocker", {}) or {},
        "inbox_window_days": INBOX_RESPONSE_WINDOW_DAYS,
        "responses": [
            {
                "email": r.email, "name": r.name, "climber_id": r.climber_id,
                "answered_at": r.answered_at, "matched": r.matched,
                "q1_overall": r.q1_overall, "q2_intent": r.q2_intent,
                "q3_blocker_raw": r.q3_blocker_raw, "q4_text": r.q4_text,
                "blocker": r.blocker, "intent": r.intent, "safety": r.safety,
            }
            for r in responses
            if _within_inbox_window(getattr(r, "answered_at", ""))
        ],
    }


def build_sent_log(sent_rows: list | None, ds: Dataset) -> list[dict]:
    """Flatten the outreach log into the rows the Recovery Queue export reads:
    one record per real send (date, climber name, email, message type).

    `sent_rows` are raw outreach-log dicts (outreach.load_rows). Test dummies
    (mode='test') are excluded so a client-facing export only shows real sends.
    The name is resolved from the dataset by climber_id when available (the log
    stores the email + id, not the display name). Newest send first."""
    rows: list[dict] = []
    for r in sent_rows or []:
        if (r.get("mode") or "").strip() == "test":
            continue
        cid = (r.get("climber_id") or "").strip()
        c = ds.climbers.get(cid) if cid else None
        rows.append({
            "sent_date": (r.get("sent_date") or "").strip()[:10],
            "name": (c.name if c else "") or "",
            "email": (r.get("email") or "").strip(),
            "trigger_name": (r.get("trigger_name") or "").strip(),
            "tag": (r.get("tag") or "").strip(),
            "mode": (r.get("mode") or "").strip(),
        })
    rows.sort(key=lambda x: x["sent_date"], reverse=True)
    return rows


def build_payload(ds: Dataset, queue: list[QueueItem], client: ClientConfig,
                  asof: date, mode: str, generated_at: str,
                  survey_result=None, suppressed_out: list | None = None,
                  sent_rows: list | None = None) -> dict:
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
        "sent_log": build_sent_log(sent_rows, ds),
        "metrics": build_metrics(ds, queue, client, asof),
        "funnel": build_funnel(ds),
        "reporting": build_reporting(ds, client, asof),
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
