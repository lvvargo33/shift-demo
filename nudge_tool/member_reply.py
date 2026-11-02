"""Replying to a membership-survey response: the in-app reply (a person clicks
Send on the response page) and the positive auto-reply (the cron's thank-you).

Membership Surveys SPEC, "Session 2026-08-21 (eve): in-app reply + auto-reply".
Read that section first; this module is its implementation.

TWO TRIGGER NAMES, BOTH IN THE OUTREACH LOG
Every reply that goes out is one outreach row, exactly like a survey send or an
FTV nudge, so the log stays the single record of "what did we email whom". The
row's `climber_id` is the response's `mid`, `tag` is the template id, and
`trigger_name` says who sent it:
  member_reply        a person, from the dashboard (POST /reply)
  member_auto_reply   the cron, the thank-you for an all-9+ response
Neither name is `member_survey`, so a reply never touches the 180-day survey
cooldown (that lookup is keyed on trigger_name). A reply DOES count for the
S38 spacing guard (last send of any kind), which is the right call: it is a
real email to that inbox.

SENT AS-IS, SIGNED "THE TEAM"
Luke 2026-08-21: staff cannot edit the copy before it goes (what the page
previews is byte-identical to what the cron would render), and every reply is
signed with `links.manager_name` ("The team", FINAL). No per-send signature
override. Staff CAN pick a different template than Chris's rule suggested.

THE AUTO-REPLY IS SMALL ON PURPOSE
Only `member_positive_v1`, only once per response (a second survey 180 days
later is a new response and earns a new thank-you), only within
AUTO_MAX_AGE_DAYS of the answer (a thank-you for something said a month ago
reads as a mistake), only behind BOTH `member_survey.enabled` and
`member_survey.auto_reply`, and never to an unmatched (test) mid or a
suppressed address. Every low score waits for a person; that was the 2026-07-16
call with Chris and it has not changed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from . import member_responses as mr
from . import member_survey
from .templates import render as render_template

REPLY_TRIGGER = "member_reply"
AUTO_TRIGGER = "member_auto_reply"
REPLY_TRIGGERS = (REPLY_TRIGGER, AUTO_TRIGGER)

# The cron will not thank someone for an answer older than this. Protects the
# day the gate flips (or a long cron outage) from mailing a pile of stale
# thank-yous at once.
AUTO_MAX_AGE_DAYS = 14

# What the page substitutes for the member's first name when it previews a
# template the server did not render for that member (the picker). Plain
# string replace in templates.render, so this comes back verbatim.
FIRST_NAME_SENTINEL = "%%FIRST_NAME%%"

# Picker labels. Chris's template titles, ours for value.
TEMPLATE_TITLE = {
    mr.POSITIVE_TEMPLATE: "Thank-you (asks for a Google review)",
    "member_low_remain_v1": "Follow-up: Likelihood to Remain",
    "member_low_staff_v1": "Follow-up: Staff Friendliness",
    "member_low_community_v1": "Follow-up: Sense of Community",
    "member_low_routes_v1": "Follow-up: Route Setting",
    "member_low_cleanliness_v1": "Follow-up: Cleanliness",
    "member_low_value_v1": "Follow-up: Value for Money",
}


class ReplyError(ValueError):
    """A bad reply request (unknown template, no email). Never raised on read."""


def reply_templates() -> list[str]:
    """The seven templates a reply may use, positive first then Chris's
    SELECTION_METRICS order. Anything else is refused: a reply must be one of
    the member response templates, never the survey invite or an FTV nudge."""
    return [mr.POSITIVE_TEMPLATE] + [mr.METRIC_TEMPLATE[k] for k in mr.SELECTION_METRICS]


def title_of(template_key: str) -> str:
    return TEMPLATE_TITLE.get(template_key, template_key)


# --------------------------------------------------------------------------
# reading: what has already been sent
# --------------------------------------------------------------------------

def replies_by_mid(outreach_rows) -> dict:
    """Outreach rows -> {mid: [reply, ...]} sorted by date, live rows only.
    reply = {date, template_key, trigger, auto}. Keyed on climber_id, which a
    reply row always carries (it is the response's mid)."""
    out: dict = {}
    for r in outreach_rows:
        trig = (r.get("trigger_name") or "").strip()
        if trig not in REPLY_TRIGGERS:
            continue
        if (r.get("mode") or "").strip() == "test":
            continue
        mid = (r.get("climber_id") or "").strip()
        d = (r.get("sent_date") or "").strip()[:10]
        if not mid or not d:
            continue
        out.setdefault(mid, []).append({
            "date": d,
            "template_key": (r.get("tag") or "").strip(),
            "trigger": trig,
            "auto": trig == AUTO_TRIGGER,
        })
    for k in out:
        out[k].sort(key=lambda x: (x["date"], x["auto"]))
    return out


def replied_since(replies, answered_at: str) -> list:
    """The replies sent on or after the day a response was answered. An older
    reply belongs to a previous survey. (Open-ended: a later survey's reply is
    included too; see replies_for.)"""
    day = (answered_at or "")[:10]
    return [x for x in (replies or []) if not day or x["date"] >= day]


def replies_for(replies, answered_at: str, next_answered_at: str = "") -> list:
    """The replies that answer THIS survey (Block 20 decision 18): sent on or
    after the day it was answered and before the day the same member answered
    their NEXT survey. With no next survey it is replied_since."""
    day = (answered_at or "")[:10]
    nxt = (next_answered_at or "")[:10]
    return [x for x in (replies or [])
            if (not day or x["date"] >= day) and (not nxt or x["date"] < nxt)]


# --------------------------------------------------------------------------
# sending one reply
# --------------------------------------------------------------------------

def log_row(mid: str, email: str, template_key: str, today: str,
            trigger: str = REPLY_TRIGGER, mode: str = "live") -> dict:
    """One outreach-log row. Matches outreach.FIELDS."""
    return {
        "sent_date": today,
        "email": (email or "").strip().lower(),
        "climber_id": mid,
        "trigger_name": trigger,
        "tag": template_key,
        "mode": mode,
        # dropped by a log with no `esp` column (ABC); SHIFT's stats push
        # reads blank as Mailchimp, and every member reply goes through Brevo
        "esp": "brevo",
    }


def fill_name(text: str, first_name: str) -> str:
    """Swap the page's first-name sentinel for the member's name (or "there",
    the same default templates.render uses). Staff-edited copy may still
    carry the sentinel if they typed it or pasted from the picker."""
    return (text or "").replace(FIRST_NAME_SENTINEL, (first_name or "").strip() or "there")


def rendered_for(client, template_key: str, first_name: str) -> tuple[str, str]:
    """(subject, body) exactly as the unedited send path would mail it for
    this member. The "original" half of a saved edit pair. Raises ReplyError
    if the template is not installed."""
    if template_key not in client.templates:
        raise ReplyError(f"template {template_key!r} is not installed for this client")
    return render_template(client.templates, template_key, first_name or "", client.links)


def send(mc, client, *, mid: str, email: str, first_name: str,
         template_key: str, today: str, trigger: str = REPLY_TRIGGER,
         dry_run: bool = False, subject: str | None = None,
         body: str | None = None) -> dict:
    """Send ONE reply and return the outreach row to append.

    Unedited (subject and body both None): the production render path
    (`mc.send_email` renders the template with client.links, so the signature
    is links.manager_name, "The team").

    Edited (roadmap block 3, 2026-09): `subject` and `body` are the text a
    staffer approved on the response page. They go out as given (after the
    first-name sentinel is filled) through `mc.send_custom`, which keeps the
    same From, shell and template tag. Both must be non-blank; an ESP without
    `send_custom` (Mailchimp) refuses before any network call.

    Raises ReplyError before any network call if the template is not one of
    the seven or is not installed, or the member has no email. ESP errors
    propagate; the caller decides how to report them. The row is returned,
    not written: the caller appends it ONLY after a successful send, the same
    rule member_send follows (a failed send must not look like a reply)."""
    mid = (mid or "").strip()
    email = (email or "").strip().lower()
    if not mid:
        raise ReplyError("mid is required")
    if template_key not in reply_templates():
        raise ReplyError(f"{template_key!r} is not a member reply template")
    if template_key not in client.templates:
        raise ReplyError(f"template {template_key!r} is not installed for this client")
    if not email:
        raise ReplyError("this member has no email address on file")
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
    return log_row(mid, email, template_key, today, trigger=trigger)


def preview_templates(client) -> list:
    """Every reply template rendered ONCE with the first-name sentinel, for the
    page's picker. [{key, title, subject, body, installed}]. The page swaps the
    sentinel for the member's first name; everything else (gym name, review
    link, signature) is already the real value from client.links, so the
    preview matches what `send` will mail."""
    out = []
    for key in reply_templates():
        item = {"key": key, "title": title_of(key), "subject": "", "body": "",
                "installed": key in client.templates}
        if item["installed"]:
            subject, body = render_template(client.templates, key,
                                            FIRST_NAME_SENTINEL, client.links)
            item["subject"], item["body"] = subject, body
        out.append(item)
    return out


# --------------------------------------------------------------------------
# the auto-reply (cron)
# --------------------------------------------------------------------------

def auto_enabled(client) -> bool:
    """`member_survey.auto_reply`, default True. A second switch under the
    survey gate so ABC can keep surveys running and turn only the automatic
    thank-you off (the B3 toggle from the 2026-07-21 plan)."""
    ms = getattr(client, "member_survey", None) or {}
    return bool(ms.get("auto_reply", True))


@dataclass
class AutoResult:
    """What the cron did (or would do), in a shape the report can print."""
    queue: list = field(default_factory=list)     # candidate dicts
    sent: list = field(default_factory=list)
    failed: list = field(default_factory=list)    # (candidate, error string)
    rows: list = field(default_factory=list)      # outreach rows to append
    reasons: list = field(default_factory=list)   # why nothing happened
    skipped: dict = field(default_factory=dict)   # reason -> count

    def skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1


def _days_old(answered_at: str, today: str) -> int:
    try:
        return (date.fromisoformat(today[:10]) - date.fromisoformat(answered_at[:10])).days
    except (ValueError, TypeError):
        return 10 ** 6      # unparseable = too old, never mail on a guess


def build_auto(client, read_result, members: dict, outreach_rows, today: str,
               unsubscribed=None, max_age_days: int = AUTO_MAX_AGE_DAYS) -> AutoResult:
    """Who gets the automatic thank-you today. Sends nothing.

    `members` is member_views.member_index (mid -> {email, first_name, ...}).
    A candidate is a matched response whose Chris-rule pick is the positive
    template, answered within `max_age_days`, with a usable, non-suppressed
    email, and no reply (human or auto) on record since the answer."""
    res = AutoResult()
    cfg = member_survey.load_config(client)
    if not cfg.enabled:
        res.reasons.append("member_survey.enabled is false")
        return res
    if not auto_enabled(client):
        res.reasons.append("member_survey.auto_reply is false")
        return res
    if mr.POSITIVE_TEMPLATE not in client.templates:
        res.reasons.append(f"template {mr.POSITIVE_TEMPLATE!r} not installed")
        return res
    if read_result is None:
        res.reasons.append("response sheet not read")
        return res

    supp = {e.strip().lower() for e in (unsubscribed or set())}
    replies = replies_by_mid(outreach_rows)
    for r in read_result.responses:
        m = members.get(r.mid)
        if m is None:
            res.skip("unmatched")            # test rows, or a member not in the file
            continue
        rec = r.recommend(cfg.positive_threshold)
        if rec is None or rec.template_key != mr.POSITIVE_TEMPLATE:
            continue                         # a person replies to these
        email = (m.get("email") or "").strip().lower()
        if not email:
            res.skip("no_email")
            continue
        if email in supp:
            res.skip("suppressed")
            continue
        if replied_since(replies.get(r.mid, []), r.answered_at):
            res.skip("already_replied")
            continue
        if _days_old(r.answered_at, today) > max_age_days:
            res.skip("too_old")
            continue
        res.queue.append({"mid": r.mid, "email": email,
                          "first_name": m.get("first_name") or "",
                          "answered_at": r.answered_at})
    res.queue.sort(key=lambda c: (c["answered_at"], c["mid"]))
    return res


def send_auto(mc, client, res: AutoResult, today: str, dry_run: bool = False) -> AutoResult:
    """Send the queue built by `build_auto`. One bad address must not abort the
    rest; failures are reported and NOT logged, so the member gets the
    thank-you on the next run instead of never."""
    for c in res.queue:
        try:
            row = send(mc, client, mid=c["mid"], email=c["email"],
                       first_name=c["first_name"], template_key=mr.POSITIVE_TEMPLATE,
                       today=today, trigger=AUTO_TRIGGER, dry_run=dry_run)
            res.sent.append(c)
            res.rows.append(row)
        except Exception as e:                                 # noqa: BLE001
            res.failed.append((c, f"{type(e).__name__}: {e}"))
    return res


def auto_report_lines(res: AutoResult) -> list[str]:
    """Lines for the daily report email."""
    if res.reasons:
        return ["  auto thank-you: none (" + "; ".join(res.reasons) + ")"]
    out = [f"  auto thank-you: {len(res.queue)} to send"]
    if res.skipped:
        bits = [f"{v} {k}" for k, v in sorted(res.skipped.items())]
        out.append("    skipped: " + ", ".join(bits))
    if res.sent:
        out.append(f"    sent {len(res.sent)}")
    for c, err in res.failed:
        out.append(f"    FAIL {c['email']}: {err}")
    return out
