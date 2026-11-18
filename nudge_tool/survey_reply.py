"""Replying to a FIRST-TIMER survey answer from the dashboard.

ROADMAP "First-timer survey responses in the app: match the Members pages"
(interviewed 2026-11-17, built 2026-11-18). The member version is
member_reply.py; this module follows it and borrows its small helpers rather
than forking them (the first-name sentinel, fill_name, the outreach row shape).

ONE TRIGGER NAME IN THE OUTREACH LOG
Every reply is one outreach row: `trigger_name` = survey_reply, `tag` = the
template id, `climber_id` = the climber the answer matched ("" when the email
matched nobody), `email` = where it went. The name is in
engine.INTERNAL_TRIGGERS, so a reply is never a first-timer "automation" in
the stats, and it is no trigger in client.json, so it can never satisfy or
block a once-only / cooldown / group rule: who gets which automatic email does
not change (Luke 2026-11-17: a reply does not pause the series).

SAME-DAY GUARD ONLY
The automatic series skips a person on the DAY a staff reply went to them and
picks them up the next day (engine.build_queue). It does NOT apply the gym's
full min_days_between_sends to a reply: with ABC's 2-day spacing a reply on
day 6 would push the day 5-7 offer past its window and the offer would never
go, which is the opposite of the ruling.

THE PICK IS A SUGGESTION
safety answer            -> no template at all (handle it in person)
a named main issue       -> that issue's template
low overall, no issue    -> the general "tell us more" template
anything else            -> the thank-you
Staff may choose any of the templates and change the words before sending.
The copy lives in clients/<slug>/templates/ftv_reply_*.md, so Chris's
rewrites drop in without a code change.
"""
from __future__ import annotations

from . import member_reply
from .templates import render as render_template

REPLY_TRIGGER = "survey_reply"
TEMPLATE_PREFIX = "ftv_reply_"

FIRST_NAME_SENTINEL = member_reply.FIRST_NAME_SENTINEL
fill_name = member_reply.fill_name

THANKS_TEMPLATE = "ftv_reply_thanks_v1"
LOW_TEMPLATE = "ftv_reply_low_v1"

# survey.blocker_map value -> its reply template
BLOCKER_TEMPLATE = {
    "too_expensive": "ftv_reply_pricing_v1",
    "too_busy": "ftv_reply_crowding_v1",
    "too_hard": "ftv_reply_too_hard_v1",
    "intimidating": "ftv_reply_intimidating_v1",
    "front_desk": "ftv_reply_frontdesk_v1",
    "confusing": "ftv_reply_confusing_v1",
    "not_enjoyable": "ftv_reply_routes_v1",
}

TEMPLATE_TITLE = {
    THANKS_TEMPLATE: "Thank-you (a good visit)",
    LOW_TEMPLATE: "Low score, no issue named",
    "ftv_reply_pricing_v1": "Main issue: too expensive",
    "ftv_reply_crowding_v1": "Main issue: too busy",
    "ftv_reply_too_hard_v1": "Main issue: too hard",
    "ftv_reply_intimidating_v1": "Main issue: intimidating",
    "ftv_reply_frontdesk_v1": "Main issue: front desk",
    "ftv_reply_confusing_v1": "Main issue: confusing",
    "ftv_reply_routes_v1": "Main issue: the climbs",
}

# The form's overall answer is 1 to 5; this or lower is a low score (the same
# line the Home page draws, dashboard_template lowOverall).
LOW_OVERALL = 3


class ReplyError(ValueError):
    """A bad reply request (unknown template, no email). Never raised on read."""


def reply_templates() -> list[str]:
    """The templates a reply may use: thank-you, general low, then one per
    main issue. Anything else is refused."""
    return [THANKS_TEMPLATE, LOW_TEMPLATE] + list(BLOCKER_TEMPLATE.values())


def title_of(template_key: str) -> str:
    return TEMPLATE_TITLE.get(template_key, template_key)


def is_reply_template(template_key: str) -> bool:
    return (template_key or "") in reply_templates()


def overall_score(q1) -> int | None:
    """The 1-5 overall answer as a number ('5', '5 - Exceptional', 5.0)."""
    s = str(q1 if q1 is not None else "").strip()
    digits = ""
    for ch in s:
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    if not digits:
        return None
    n = int(digits)
    return n if 1 <= n <= 5 else None


def suggest(*, safety: bool, blocker, q1_overall) -> dict:
    """The suggested template for one answer. {template_key, title, reason};
    template_key is "" for a safety answer (no suggested reply)."""
    if safety:
        return {"template_key": "", "title": "",
                "reason": "This answer raises a safety concern. Handle it in person; "
                          "no reply is suggested."}
    b = (blocker or "").strip()
    if b in BLOCKER_TEMPLATE:
        key = BLOCKER_TEMPLATE[b]
        return {"template_key": key, "title": title_of(key),
                "reason": "They named a main issue, so the reply speaks to that issue."}
    n = overall_score(q1_overall)
    if n is not None and n <= LOW_OVERALL:
        return {"template_key": LOW_TEMPLATE, "title": title_of(LOW_TEMPLATE),
                "reason": f"They scored the visit {n} out of 5 and named no issue, "
                          "so the reply asks what happened."}
    return {"template_key": THANKS_TEMPLATE, "title": title_of(THANKS_TEMPLATE),
            "reason": "No issue named and no low score, so the reply is a thank-you."}


def preview_templates(client) -> list:
    """Every reply template rendered ONCE with the first-name sentinel, for the
    page's picker: [{key, title, subject, body, installed}]."""
    out = []
    for key in reply_templates():
        item = {"key": key, "title": title_of(key), "subject": "", "body": "",
                "installed": key in client.templates}
        if item["installed"]:
            item["subject"], item["body"] = render_template(
                client.templates, key, FIRST_NAME_SENTINEL, client.links)
        out.append(item)
    return out


def rendered_for(client, template_key: str, first_name: str) -> tuple[str, str]:
    """(subject, body) as the unedited template reads for this person."""
    if template_key not in client.templates:
        raise ReplyError(f"template {template_key!r} is not installed for this client")
    return render_template(client.templates, template_key, first_name or "", client.links)


# --------------------------------------------------------------------------
# reading: what has already been sent
# --------------------------------------------------------------------------

def replies_by_email(outreach_rows) -> dict:
    """Outreach rows -> {email: [reply, ...]} sorted by date, live rows only.
    reply = {date, template_key, trigger, auto}. Keyed on the email because an
    answer is matched to a climber BY email and some answers match nobody."""
    out: dict = {}
    for r in outreach_rows or []:
        if (r.get("trigger_name") or "").strip() != REPLY_TRIGGER:
            continue
        if (r.get("mode") or "").strip() == "test":
            continue
        em = (r.get("email") or "").strip().lower()
        d = (r.get("sent_date") or "").strip()[:10]
        if not em or not d:
            continue
        out.setdefault(em, []).append({
            "date": d, "template_key": (r.get("tag") or "").strip(),
            "trigger": REPLY_TRIGGER, "auto": False})
    for k in out:
        out[k].sort(key=lambda x: x["date"])
    return out


def replies_for(replies, answered_at: str, next_answered_at: str = "") -> list:
    """The replies that answer THIS survey: sent on or after the day it was
    answered and before the same person's next answer (member_reply's rule)."""
    return member_reply.replies_for(replies, answered_at, next_answered_at)


def emailed_on(outreach_rows, email: str, day: str) -> list:
    """Live outreach rows to this email on `day` (any trigger): the same-day
    check the reply route makes before a send."""
    em = (email or "").strip().lower()
    out = []
    for r in outreach_rows or []:
        if (r.get("mode") or "").strip() == "test":
            continue
        if (r.get("email") or "").strip().lower() != em:
            continue
        if (r.get("sent_date") or "").strip()[:10] == day:
            out.append(r)
    return out


# --------------------------------------------------------------------------
# sending one reply
# --------------------------------------------------------------------------

def log_row(climber_id: str, email: str, template_key: str, today: str,
            mode: str = "live") -> dict:
    return member_reply.log_row(climber_id or "", email, template_key, today,
                                trigger=REPLY_TRIGGER, mode=mode)


def send(mc, client, *, climber_id: str, email: str, first_name: str,
         template_key: str, today: str, subject: str | None = None,
         body: str | None = None, dry_run: bool = False) -> dict:
    """Send ONE reply and return the outreach row to append (the caller writes
    it only after a successful send). Unedited = the template as rendered;
    edited = the staffer's subject + body through `mc.send_custom`. Raises
    ReplyError before any network call on a bad request."""
    email = (email or "").strip().lower()
    if not is_reply_template(template_key):
        raise ReplyError(f"{template_key!r} is not a first-timer reply template")
    if template_key not in client.templates:
        raise ReplyError(f"template {template_key!r} is not installed for this client")
    if not email:
        raise ReplyError("this answer has no email address")
    edited = subject is not None or body is not None
    if edited:
        subject = fill_name(subject or "", first_name).strip()
        body = fill_name(body or "", first_name).strip()
        if not subject or not body:
            raise ReplyError("the subject and the email text cannot be blank")
        if not hasattr(mc, "send_custom"):
            raise ReplyError("editing the copy is not supported by this email service")
    if not dry_run:
        mc.upsert_member(email, first_name or "")
        if edited:
            mc.send_custom(email, first_name or "", template_key, subject, body)
        else:
            mc.send_email(email, first_name or "", template_key)
    return log_row(climber_id, email, template_key, today)
