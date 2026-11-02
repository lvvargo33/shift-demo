"""The `members` block of the dashboard payload: everything the Members views
(Overview, Responses, Recognition, Insights, Settings) need, computed in Python
so the JavaScript only formats.

Membership Surveys SPEC, "Session 2026-08-21 (late pm)", payload contract.
Read that section first; this module is its implementation.

DESIGN NOTES
- Pure functions over already-loaded inputs (`build`) plus one convenience
  (`build_live`) that reads the sheet, the member file, the outreach log and
  the follow-up CSV. Keeping `build` pure is what makes the views testable
  without Google.
- FAILS OPEN FOR THE PAGE, LOUD IN THE DATA. A sheet read error must not take
  the whole dashboard down (the FTV queue is live and this section is not), so
  `build_live` catches it and ships `sheet_ok: false` + the error text; the UI
  says so instead of silently showing zero responses.
- Responses whose `mid` matches no member (the test rows: LUKETEST x2,
  test-20260821-..., TEST123) are COUNTED in `responses_unmatched` and never
  listed, so the views cannot show a fake member.
- Workflow rules are Lovable's `members-workflow.ts` where it had a real rule
  (retentionRisk, priority, statuses, attention ordering) and ours where it had
  a mock (due dates were a hash there; here they are answered_at + 2/5/10 days
  by risk, open question 21). Nothing AI-flavoured is ported.
"""
from __future__ import annotations

import csv
from datetime import date, timedelta

from . import (audience, checkin_alert, member_counts, member_followups,
               member_reply, member_reply_edits, member_responses, member_survey)
from .config import ClientConfig, load_settings
from .templates import render as render_template

# Lovable `retentionRisk`: remain is their `retention`.
RISK_HIGH = {"remain": 4, "nps": 3}
RISK_MEDIUM = {"remain": 6, "nps": 6}

# Due-date offsets (days after the response) by risk. Ours, not Chris's:
# Lovable's dueDate() was a hash for the mock. Open question 21.
DUE_DAYS = {"high": 2, "medium": 5, "low": 10}

# Display labels for the Q5/Q6 diagnostic rows (staff-only context on the
# response page and the Insights diagnostics slide). Selection labels come
# from member_responses.METRIC_LABEL.
DIAGNOSTIC_LABEL = {
    "grade_range": "Range of climbs at their level",
    "reset_frequency": "How often climbs get reset",
    "crowding": "Getting on climbs without waiting",
    "training_area": "Training and fitness area",
    "staff_help": "Getting help from staff",
    "info_access": "Finding out what's happening",
    "safety": "Feeling safe on the wall",
}

# The five Q4 dimensions, in selection order, minus `remain` (Q3 is shown as
# its own "stay 6 months" number, like the demo's `retention`).
DIMS = [(k, member_responses.METRIC_LABEL[k])
        for k in member_responses.SELECTION_METRICS if k != "remain"]
DIAGNOSTICS = [(k, DIAGNOSTIC_LABEL[k]) for k in member_responses.DIAGNOSTIC_METRICS]


# --------------------------------------------------------------------------
# inputs
# --------------------------------------------------------------------------

def member_index(path) -> dict:
    """ALL rows of member_status.csv keyed by climber_key (any status).

    Deliberately wider than `audience.select`, which drops doNotMail and
    household duplicates and non-OK statuses: a member who answered and then
    cancelled, or the non-winning half of a household, must still show a name
    on their response. Missing file -> {}."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    except (OSError, TypeError):
        return {}
    out = {}
    for r in rows:
        key = str(r.get("climber_key") or "").strip()
        if key:
            out[key] = {
                "first_name": str(r.get("first_name") or "").strip(),
                "last_name": str(r.get("last_name") or "").strip(),
                "email": str(r.get("email") or "").strip().lower(),
                "status": str(r.get("status") or "").strip(),
                "membership_start": audience._clean_date(r.get("membership_start")),
            }
    return out


def survey_sends(outreach_rows) -> dict:
    """Outreach rows -> {key: [sent_date, ...] sorted} for the member-survey
    trigger, live rows only. Keyed by climber_id when present, else email, so
    a response (which carries mid = climber_id) can find its sends. The rule
    itself moved to member_responses (the parser needs it to tell rounds
    apart, Block 20 decision 18); this name stays for its callers."""
    return member_responses.survey_sends(outreach_rows)


# What each member-facing outreach row is, for the gym-wide communications log
# (Block 20 decision 9). trigger_name -> (kind, label, who). The staff reply
# label gets the template title appended at build time.
COMMS_KINDS = {
    member_survey.TRIGGER_NAME: ("survey", "Survey invite", "Automatic"),
    member_reply.AUTO_TRIGGER: ("auto_reply", "Automatic thank-you", "Automatic"),
    member_reply.REPLY_TRIGGER: ("reply", "Reply from staff", "Staff"),
    checkin_alert.TRIGGER_NAME: ("checkin_alert", "Front-desk check-in alert (to the gym inbox)", "Automatic"),
    # SHIFT's day-14 new-member email (member_checkin.TRIGGER_NAME); no rows at ABC
    "member_check_in": ("check_in", "Day-14 check-in email", "Automatic"),
}
COMMS_DAYS = 90


def comms_log(outreach_rows, members: dict, today: str, days: int = COMMS_DAYS) -> dict:
    """The gym-wide member communications log (decision 9, Luke 2026-10-05):
    every message that went out about a member, newest first, with who sent it
    (Automatic vs Staff; no staff names, everyone shares one login). Rows
    older than `days` carry recent=False so the page can fold them behind
    "Show older". Test-mode rows never show. The outreach log is the only
    source: every survey invite, thank-you, staff reply and check-in alert is
    one row there by construction."""
    try:
        cutoff = (date.fromisoformat(today[:10]) - timedelta(days=days)).isoformat()
    except ValueError:
        cutoff = ""
    rows = []
    for r in outreach_rows or []:
        trig = (r.get("trigger_name") or "").strip()
        kind = COMMS_KINDS.get(trig)
        if kind is None or (r.get("mode") or "").strip() == "test":
            continue
        d = (r.get("sent_date") or "").strip()[:10]
        if not d:
            continue
        mid = (r.get("climber_id") or "").strip()
        em = (r.get("email") or "").strip().lower()
        m = members.get(mid) if mid else None
        name = (" ".join(p for p in ((m or {}).get("first_name", ""), (m or {}).get("last_name", "")) if p)
                if m else "")
        if not name:
            # A member who left the member file (cancelled) has no name here.
            # The alert row's email is the GYM inbox, never the member's, so
            # it must not stand in for the name (tester 2026-10-08).
            name = "Member no longer on file" if kind[0] == "checkin_alert" else (em or mid)
        label = kind[1]
        if kind[0] == "reply":
            label = f"{label}: {member_reply.title_of((r.get('tag') or '').strip())}"
        rows.append({"date": d, "mid": mid, "email": em, "name": name,
                     "kind": kind[0], "label": label, "who": kind[2],
                     "recent": bool(cutoff and d >= cutoff)})
    rows.sort(key=lambda x: (x["date"], x["name"]), reverse=True)
    return {"rows": rows, "days": days,
            "recent": sum(1 for x in rows if x["recent"]), "total": len(rows)}


# --------------------------------------------------------------------------
# the member profile timeline (Block 20 R1b, decisions 15-17)
# --------------------------------------------------------------------------

# The member's own survey link carries their id; the timeline does not know
# which link went out, so the invite shows this in its place.
SURVEY_LINK_STANDIN = "[their own survey link]"

# Never on a member's timeline: the front-desk alert goes to the gym inbox,
# not the member (decision 16).
TIMELINE_SKIP = {checkin_alert.TRIGGER_NAME}


def timeline_templates(client: ClientConfig) -> dict:
    """Every installed template rendered ONCE with the first-name sentinel, for
    the timeline's automatic emails (the page swaps in the member's name, the
    same way the reply picker does). {template_id: {subject, subject_b, body}}.
    The member survey link is per-recipient, so it shows as a stand-in."""
    links = dict(client.links)
    links.setdefault("member_survey_link", SURVEY_LINK_STANDIN)
    out = {}
    for key in sorted(client.templates):
        subject, body = render_template(client.templates, key,
                                        member_reply.FIRST_NAME_SENTINEL, links)
        subject_b = ""
        if client.templates[key].subject_b:
            subject_b, _ = render_template(client.templates, key,
                                           member_reply.FIRST_NAME_SENTINEL,
                                           links, variant="B")
        out[key] = {"subject": subject, "subject_b": subject_b, "body": body}
    return out


def _ab_variant(email: str, template_id: str) -> str:
    """The subject this person got when the template runs an A/B subject test:
    the Brevo send path's own deterministic bucket (email + template id), so
    the timeline shows the subject they actually received."""
    from .brevo_client import BrevoClient
    return BrevoClient._ab_variant(email, template_id)


def _match_edits(reply_edit_rows) -> dict:
    """{mid: [edit row, ...]} sorted by time, for matching a staff reply in the
    outreach log to the words that were actually sent."""
    out: dict = {}
    for e in reply_edit_rows or []:
        mid = (e.get("mid") or "").strip()
        if mid:
            out.setdefault(mid, []).append(e)
    for k in out:
        out[k].sort(key=lambda e: e.get("ts") or "")
    return out


def _day_gap(a: str, b: str) -> int:
    try:
        return abs((date.fromisoformat(a[:10]) - date.fromisoformat(b[:10])).days)
    except ValueError:
        return 99


def member_timelines(client: ClientConfig, outreach_rows, responses: list,
                     reply_edit_rows=None) -> dict:
    """{mid: [item, ...]} newest first, for every member who answered a survey
    (the profile is reached from a survey, decision 15).

    What is on it (decision 16): member survey invites, automatic thank-yous,
    staff replies, every first-time-visitor email sent to the same climber id
    or email before or after they joined, and each survey they answered. Not
    on it: front-desk check-in alerts, follow-up notes, test-mode rows.

    Each email opens to its words (decision 17): a staff reply carries the
    exact subject + body from member_reply_edits.csv (same member, same
    template, sent within a day of the log row; each saved pair used once);
    anything else (automatic emails, replies sent before the edit box) points
    at today's template copy and says so (`source: "template"`).

    item = {date, kind, trigger, label, who, template_key, title, variant,
            subject, body, source, rid, round}   (title: a staff reply's template)
      kind    survey_invite | auto_reply | reply | visitor_email | answered
      source  sent (exact words) | template (today's copy) | missing (no copy
              on file) | "" (an answered survey: opens the survey itself)
    The page renders `body` only for source=sent; for source=template it uses
    members.timeline_templates[template_key] with the first name swapped in.
    """
    trig_template = {t.name: t.template_id for t in client.triggers}
    tag_template = {t.tag: t.template_id for t in client.triggers}
    survey_tpl = member_survey.load_config(client).template_id
    edits = _match_edits(reply_edit_rows)
    used: set = set()

    by_mid: dict = {}
    for r in responses:
        by_mid.setdefault(r["mid"], {"email": r["email"], "first": r["first_name"],
                                     "resps": []})["resps"].append(r)
    by_email = {v["email"]: k for k, v in by_mid.items() if v["email"]}

    out: dict = {k: [] for k in by_mid}
    for row in outreach_rows or []:
        trig = (row.get("trigger_name") or "").strip()
        if not trig or trig in TIMELINE_SKIP or (row.get("mode") or "").strip() == "test":
            continue
        d = (row.get("sent_date") or "").strip()[:10]
        if not d:
            continue
        cid = (row.get("climber_id") or "").strip()
        em = (row.get("email") or "").strip().lower()
        mid = cid if cid in by_mid else by_email.get(em)
        if mid is None:
            continue
        tag = (row.get("tag") or "").strip()
        item = {"date": d, "trigger": trig, "template_key": "", "title": "", "variant": "",
                "subject": "", "body": "", "source": "template", "rid": "", "round": 0}
        if trig == member_survey.TRIGGER_NAME:
            item.update(kind="survey_invite", label="Survey invite", who="Automatic",
                        template_key=survey_tpl)
        elif trig == member_reply.AUTO_TRIGGER:
            item.update(kind="auto_reply", label="Automatic thank-you", who="Automatic",
                        template_key=tag)
        elif trig == member_reply.REPLY_TRIGGER:
            item.update(kind="reply", label="Reply from staff", who="Staff",
                        template_key=tag, title=member_reply.title_of(tag))
            best = None
            for i, e in enumerate(edits.get(mid, [])):
                key = (mid, i)
                if key in used or (e.get("template_key") or "").strip() != tag:
                    continue
                gap = _day_gap(e.get("ts") or "", d)
                if gap <= 1 and (best is None or gap < best[0]):
                    best = (gap, key, e)
            if best is not None:
                used.add(best[1])
                e = best[2]
                item["subject"] = e.get("edited_subject") or e.get("original_subject") or ""
                item["body"] = e.get("edited_body") or e.get("original_body") or ""
                item["source"] = "sent"
        else:
            item.update(kind="visitor_email", label="", who="Automatic",
                        template_key=trig_template.get(trig) or tag_template.get(tag) or "")
        tk = item["template_key"]
        if item["source"] == "template":
            if tk in client.templates:
                if client.templates[tk].subject_b:
                    item["variant"] = _ab_variant(em or by_mid[mid]["email"], tk)
            else:
                item["source"] = "missing"
        out[mid].append(item)

    for mid, v in by_mid.items():
        for r in v["resps"]:
            out[mid].append({"date": (r["answered_at"] or "")[:10], "trigger": "",
                             "kind": "answered", "label": f"Answered survey round {r['wave']}",
                             "who": "Member", "template_key": "", "title": "", "variant": "",
                             "subject": "", "body": "", "source": "", "rid": r["rid"],
                             "round": r["wave"]})
    # newest first; on one day: the reply, then the answer, then the invite
    # (the order they happened, reversed)
    order = {"reply": 0, "auto_reply": 0, "answered": 1}
    for mid in out:
        out[mid].sort(key=lambda x: (x["date"], -order.get(x["kind"], 2)), reverse=True)
    return out


# --------------------------------------------------------------------------
# per-response rules
# --------------------------------------------------------------------------

def risk(nps, remain) -> str:
    """Lovable retentionRisk. None scores do not count against anyone."""
    def le(v, t):
        return v is not None and v <= t
    if le(remain, RISK_HIGH["remain"]) or le(nps, RISK_HIGH["nps"]):
        return "high"
    if le(remain, RISK_MEDIUM["remain"]) or le(nps, RISK_MEDIUM["nps"]):
        return "medium"
    return "low"


def due_date(answered_at: str, risk_level: str) -> str:
    try:
        d = date.fromisoformat((answered_at or "")[:10])
    except ValueError:
        return ""
    return (d + timedelta(days=DUE_DAYS.get(risk_level, 10))).isoformat()


def issues(resp: member_responses.MemberResponse, threshold: int) -> list:
    """Lovable detectedIssues, minus its engagement rule: every selection
    metric below the positive threshold, in SELECTION_METRICS order."""
    out = []
    for k in member_responses.SELECTION_METRICS:
        v = resp.selection_scores.get(k)
        if v is not None and v < threshold:
            out.append({"key": k, "label": member_responses.METRIC_LABEL[k],
                        "score": v})
    return out


def wave_of(sends: list, answered_at: str) -> int:
    """Wave N = the Nth survey send to this person on or before the answer
    date. No send on record (a test row, or a log that has not synced) = 1.
    The same rule the parser uses to tell rounds apart (member_responses)."""
    return member_responses.round_of(sends, answered_at)


def lowest_score(scores: dict):
    """The lowest answered score across recommend, stay and the five
    dimensions, or None when nothing was scored. The Needs a reply tab sorts
    on it (decision 2: lowest scores first)."""
    vals = [v for v in scores.values() if v is not None]
    return min(vals) if vals else None


def render_draft(client: ClientConfig, template_key: str, first_name: str):
    """The real template rendered for this member, the way the send path
    would (client.links carries gym_name / manager_name / google_review_link).
    None if the template is missing, so the page says so rather than
    inventing text."""
    if not template_key or template_key not in client.templates:
        return None
    subject, body = render_template(client.templates, template_key,
                                    first_name, client.links)
    return {"template_key": template_key, "subject": subject, "body": body}


# --------------------------------------------------------------------------
# the block
# --------------------------------------------------------------------------

def build(client: ClientConfig, asof: date, *, read_result, members: dict,
          counts, outreach_rows, followup_rows, sheet_ok: bool = True,
          sheet_error: str = "", followups_ok: bool = True,
          settings=None, count_rows=None, reply_edit_rows=None,
          reply_edits_ok: bool = True) -> dict:
    """Pure: inputs in, the `members` payload block out.

    `count_rows` is the weekly member-count snapshot log (member_counts.py).
    Passing None means "no history supplied", which renders as the
    still-building state rather than an invented line.

    `reply_edit_rows` is the saved reply-edit pair log
    (member_reply_edits.py): its counts reach the Settings card, and its
    sent words are what a staff reply opens to on the member's timeline."""
    cfg = member_survey.load_config(client)
    thr = cfg.positive_threshold
    today = asof.isoformat()
    fu_state = member_followups.merge(followup_rows)
    sends = survey_sends(outreach_rows)
    replies_all = member_reply.replies_by_mid(outreach_rows)
    ms = client.member_survey or {}
    assignees = [str(a).strip() for a in (ms.get("assignees") or []) if str(a).strip()]

    # ---- sends + waves --------------------------------------------------
    live_sends = [r for r in outreach_rows
                  if (r.get("trigger_name") or "").strip() == member_survey.TRIGGER_NAME
                  and (r.get("mode") or "").strip() != "test"]
    per_person: dict = {}
    for r in live_sends:
        key = (r.get("climber_id") or "").strip() or (r.get("email") or "").strip().lower()
        if key:
            per_person.setdefault(key, []).append((r.get("sent_date") or "")[:10])
    sent_by_wave: dict = {}
    for dates in per_person.values():
        for i, _ in enumerate(sorted(dates), start=1):
            sent_by_wave[i] = sent_by_wave.get(i, 0) + 1
    sent_dates = sorted(d for ds in per_person.values() for d in ds)
    sent_block = {
        "total": len(live_sends),
        "people": len(per_person),
        "today": sum(1 for d in sent_dates if d == today),
        "last_send_date": sent_dates[-1] if sent_dates else "",
        "first_send_date": sent_dates[0] if sent_dates else "",
        "by_wave": {str(k): v for k, v in sorted(sent_by_wave.items())},
    }

    # ---- responses --------------------------------------------------------
    # One row per SURVEY, not per member (Block 20 decision 18, 2026-10-08):
    # `id` is the survey's own id (rid), a member who answered two rounds is
    # listed twice, and a reply attaches to the survey it answered (sent after
    # that answer and before the member's next one).
    all_resps = list(read_result.responses if read_result else [])
    answered_by_mid: dict = {}
    for r in all_resps:
        answered_by_mid.setdefault(r.mid, []).append(r.answered_at)
    for k in answered_by_mid:
        answered_by_mid[k].sort()

    responses = []
    unmatched = 0
    resp_by_wave: dict = {}
    for r in all_resps:
        m = members.get(r.mid)
        if m is None:
            unmatched += 1
            continue
        rec = r.recommend(thr)
        rk = risk(r.nps, r.remain)
        is_det = r.is_detractor(thr)
        wave = wave_of(sends.get(r.mid) or sends.get(m["email"]) or [], r.answered_at)
        resp_by_wave[wave] = resp_by_wave.get(wave, 0) + 1
        first = m["first_name"] or "there"
        later = [d for d in answered_by_mid.get(r.mid, []) if d > r.answered_at]
        next_answered = later[0] if later else ""
        # Shown on this survey: the replies sent after it and before the
        # member's next survey. "Heard back": ANY reply since this answer, so
        # an old survey does not sit in Needs a reply forever once a later
        # reply went out (the later survey shows that reply).
        my_replies = member_reply.replies_for(replies_all.get(r.mid, []),
                                              r.answered_at, next_answered)
        since = member_reply.replied_since(replies_all.get(r.mid, []), r.answered_at)
        last_reply = since[-1] if since else None

        fu = fu_state.get(r.mid)
        if fu is None and is_det:
            fu = {"status": member_followups.DEFAULT_STATUS, "assignee": "",
                  "notes": [], "updated_at": "", "by": ""}
        if fu is not None:
            fu = dict(fu)
            fu["due_date"] = due_date(r.answered_at, rk)
            fu["overdue"] = bool(fu["status"] != "resolved" and fu["due_date"]
                                 and fu["due_date"] < today)

        scores = {"nps": r.nps, "remain": r.remain}
        for k, _ in DIMS:
            scores[k] = r.metrics.get(k)
        diag = {k: r.metrics.get(k) for k, _ in DIAGNOSTICS}
        responses.append({
            "id": r.rid, "mid": r.mid, "rid": r.rid,
            "email": m["email"],
            "first_name": m["first_name"], "last_name": m["last_name"],
            "name": " ".join(p for p in (m["first_name"], m["last_name"]) if p) or m["email"],
            "member_status": m["status"],
            "member_since": m["membership_start"],
            "answered_at": r.answered_at,
            "started_at": r.started_at, "ended_at": r.ended_at,
            "respondent_id": r.respondent_id,
            "wave": wave,
            "scores": scores,
            "diagnostics": diag,
            "text": {"why": r.open_comment, "improve": r.improve_comment,
                     "shout_out": r.staff_shout_out},
            # A2: SHIFT's extra answers + member context ({} at ABC)
            "extra": dict(getattr(r, "extra", None) or {}),
            "partial": not any(v is not None for v in diag.values()),
            "scoreable": r.scoreable,
            "recommend": None if rec is None else {
                "template_key": rec.template_key, "metric": rec.metric,
                "metric_label": (member_responses.METRIC_LABEL.get(rec.metric)
                                 if rec.metric else None),
                "reason": rec.reason,
                "ranked": [[k, v] for k, v in rec.ranked],
            },
            "is_detractor": is_det,
            "is_promoter": bool(r.nps is not None and r.nps >= 9),
            "risk": rk, "priority": rk,
            "issues": issues(r, thr),
            "guidance": list(member_responses.INTERNAL_GUIDANCE.get(
                rec.template_key if rec else "", [])),
            "draft": render_draft(client, rec.template_key if rec else "", first),
            "fu": fu,
            # replies already sent for THIS survey (human or auto), from the
            # outreach log; `replies_all` carries the member's others too
            "replies": my_replies,
            "replies_all": list(replies_all.get(r.mid, [])),
            "replied": bool(since),
            "reply_date": last_reply["date"] if last_reply else "",
            "reply_auto": bool(last_reply and last_reply["auto"]),
            "next_answered_at": next_answered,
            "lowest_score": lowest_score(scores),
        })
    responses.sort(key=lambda x: (x["answered_at"], x["respondent_id"]), reverse=True)
    needs_reply = sum(1 for x in responses if not x["replied"])

    wave_ids = sorted(set(sent_by_wave) | set(resp_by_wave) | {1})
    waves = [{"id": w, "label": f"Wave {w}", "sent": sent_by_wave.get(w, 0),
              "responses": resp_by_wave.get(w, 0)} for w in wave_ids]

    # ---- config + identity -------------------------------------------------
    from_name = from_email = reply_to = ""
    if settings is not None:
        from_name = settings.sender_name or client.send_from_name or client.client_name
        from_email = settings.sender_email
        reply_to = settings.reply_to_email or settings.sender_email
    config = {
        "enabled": cfg.enabled,
        "daily_cap": cfg.daily_cap,
        "cooldown_days": cfg.cooldown_days,
        "positive_threshold": thr,
        "survey_url": cfg.survey_url,
        "template_id": cfg.template_id,
        "assignees": assignees,
        "from_name": from_name, "from_email": from_email, "reply_to": reply_to,
        "manager_name": str(client.links.get("manager_name", "") or ""),
        "gym_name": str(client.links.get("gym_name", "") or ""),
        "google_review_link": str(client.links.get("google_review_link", "") or ""),
        "digest_inbox": None,           # digest CUT (Luke 2026-08-21); Q18 moot
        # live front-desk check-in alert (2026-08-21 late eve). Config only;
        # live_server adds the runtime status under payload["members"]["checkin_alert"].
        "checkin_alert": checkin_alert.load_config(client).as_view(),
        "sheet_id": str(ms.get("sheet_id", "") or ""),
        # in-app reply + auto-reply (2026-08-21 eve build)
        "auto_reply": member_reply.auto_enabled(client),
        "auto_reply_max_age_days": member_reply.AUTO_MAX_AGE_DAYS,
        "reply_templates": member_reply.preview_templates(client),
        "first_name_sentinel": member_reply.FIRST_NAME_SENTINEL,
        # A2 (2026-11-02): where answers come from ("native" = SHIFT's
        # /member-survey on Firestore, else ABC's SurveyMonkey sheet) and,
        # for native, the daily share instead of a daily cap
        "source": str(ms.get("source", "") or "surveymonkey"),
        "per_day_text": (f"about 1 in {int(ms.get('spread_days') or 90)} members a day"
                         if str(ms.get("source", "") or "") == "native" else None),
    }

    aud = None
    if counts is not None:
        aud = {
            "sendable": counts.sendable,
            "excluded_do_not_mail": counts.do_not_mail_flagged,
            "disclosure_sentence": counts.ui_sentence(),
            "funnel_lines": counts.funnel_lines(),
            "active": counts.active,
            "no_email": counts.no_email,
            "do_not_mail_step": counts.do_not_mail,
            "shared_dropped": counts.shared_email_folded,
            "member_rows": counts.member_rows,
            "inactive_by_status": dict(counts.inactive_by_status),
        }

    return {
        "as_of": today,
        "enabled": cfg.enabled,
        "config": config,
        "audience": aud,
        # Chris 2026-09-02: current member count + trend on the Overview strip.
        # `current` is today's live number; `points` is only what has actually
        # been recorded week by week. See nudge_tool/member_counts.py for why
        # the history cannot be backfilled.
        "member_count": member_counts.build_block(
            client, counts, rows=(count_rows or [])),
        "sent": sent_block,
        "waves": waves,
        "dims": [list(d) for d in DIMS],
        "diagnostics": [list(d) for d in DIAGNOSTICS],
        "statuses": [[k, member_followups.STATUS_LABEL[k]]
                     for k in member_followups.STATUSES],
        "responses": responses,
        # Block 20 decision 2: surveys answered with no reply yet (the badge,
        # the Overview tile, the Home count once R2 builds Home)
        "needs_reply": needs_reply,
        # Block 20 decision 9: the gym-wide member communications log
        "comms_log": comms_log(outreach_rows, members, today),
        # Block 20 R1b (decisions 15-17): each answering member's History
        # timeline, and the template copy its automatic emails point at
        "timelines": member_timelines(client, outreach_rows, responses,
                                      reply_edit_rows),
        "timeline_templates": timeline_templates(client),
        "responses_unmatched": unmatched,
        "responses_total_rows": getattr(read_result, "total_rows", 0),
        "duplicates_dropped": getattr(read_result, "duplicates_dropped", 0),
        "blank_mid": getattr(read_result, "blank_mid", 0),
        "unknown_row_labels": sorted(getattr(read_result, "unknown_row_labels", set()) or []),
        "sheet_ok": sheet_ok,
        "sheet_error": sheet_error,
        "followups_csv_ok": followups_ok,
        "followups_rows": len(followup_rows),
        # reply edit pairs (roadmap block 3): counts only, for Settings
        "reply_edits": member_reply_edits.summary(reply_edit_rows),
        "reply_edits_csv_ok": reply_edits_ok,
        "members_known": len(members),
        "due_days": dict(DUE_DAYS),
    }


def build_live(client: ClientConfig, asof: date, outreach_rows=None) -> dict:
    """Read everything and build. Only the sheet read is allowed to fail; it
    is reported in the block. Member file missing = no names, counts None
    (the UI then says the audience file has not arrived)."""
    path = getattr(client, "member_status_csv", None)
    native = str((client.member_survey or {}).get("source", "") or "").strip() == "native"
    if native:
        # SHIFT (A2): names + status from Beta's Memberships.csv, no RGP audience
        from . import member_responses_native
        members, path = member_responses_native.member_index(client), None
    else:
        members = member_index(path) if path else {}
    counts = None
    if path and path.exists():
        try:
            _, counts = audience.load(path)
        except Exception:           # noqa: BLE001 - a bad audience file must not 500 the page
            counts = None

    if outreach_rows is None:
        from . import outreach
        outreach_rows = outreach.load_rows(client)

    read_result, sheet_ok, sheet_error = None, True, ""
    try:
        read_result = member_responses.read(client, outreach_rows=outreach_rows)
    except Exception as e:          # noqa: BLE001
        sheet_ok, sheet_error = False, f"{type(e).__name__}: {e}"
        read_result = member_responses.ReadResult()

    followups_ok, fu_rows = True, []
    try:
        fu_rows = member_followups.load_rows(client)
    except Exception as e:          # noqa: BLE001
        followups_ok = False
        sheet_error = sheet_error or f"followups: {type(e).__name__}: {e}"

    # Weekly member-count history. Missing or unreadable = no trend line, never
    # a 500: this is a nice-to-have strip on a page whose real job is responses.
    try:
        count_rows = member_counts.load_rows(client)
    except Exception:               # noqa: BLE001
        count_rows = []

    # Reply edit pairs: a count on Settings and the words on the timeline,
    # never a 500 (unreadable = every staff reply shows its template copy).
    reply_edits_ok, edit_rows = True, []
    try:
        edit_rows = member_reply_edits.load_rows(client)
    except Exception:               # noqa: BLE001
        reply_edits_ok = False

    try:
        if native:
            # SHIFT replies go through Brevo (its config default is Mailchimp)
            from . import survey_dispatch
            settings = survey_dispatch.brevo_settings(client)
        else:
            settings = load_settings(client, require=False)
    except Exception:               # noqa: BLE001
        settings = None

    return build(client, asof, read_result=read_result, members=members,
                 counts=counts, outreach_rows=outreach_rows,
                 followup_rows=fu_rows, sheet_ok=sheet_ok,
                 sheet_error=sheet_error, followups_ok=followups_ok,
                 settings=settings, count_rows=count_rows,
                 reply_edit_rows=edit_rows, reply_edits_ok=reply_edits_ok)


def wants_block(client: ClientConfig) -> bool:
    """The data-presence gate (decision 2026-08-21): the Members section
    renders only for a client whose client.json has a `member_survey` block,
    enabled or not. SHIFT has none, so its page is byte-identical."""
    return bool(getattr(client, "member_survey", None))
