"""The `ftv_replies` block of the dashboard payload: what the first-timer
survey pages need on top of today's `survey` block (a page per answer, the
suggested reply, "Needs a reply", each person's email history, the charts).

ROADMAP "First-timer survey responses in the app: match the Members pages"
(interviewed 2026-11-17, built 2026-11-18). Follows member_views.py: a pure
`build` over already-loaded inputs, so the pages are testable without Google.

THE SWITCH
client.json survey.responses_page = {"enabled": true, "needs_reply_from":
"YYYY-MM-DD"}. Absent or not enabled = no block in the payload and the page
renders today's table, byte for byte (SHIFT until its own short session).

NEEDS A REPLY
An answer needs a reply when it was given on or after `needs_reply_from`
(Luke 2026-11-17: only the last 14 days are flagged on day one; older answers
are read-only history), it is not a safety answer (those are handled in
person and get no suggested reply), it has an email, and no staff reply has
gone to that email since it was answered.
"""
from __future__ import annotations

import hashlib
from datetime import date

from . import member_views, survey_reply

# Outreach rows that never show on a first-timer's history: the front-desk
# alert goes to the gym's own inbox, not the person.
HISTORY_SKIP = {"member_checkin_alert"}

# Labels for the member-program rows a first-timer who later joined can carry.
MEMBER_LABEL = {
    "member_survey": "Member survey invite",
    "member_auto_reply": "Automatic thank-you (member survey)",
    "member_reply": "Reply from staff (member survey)",
    "member_check_in": "Day-14 member check-in email",
}


def config(client) -> dict:
    """survey.responses_page as a dict ({} when absent; `true` = enabled with
    no start date, so every answer can be replied to)."""
    raw = (getattr(client, "survey", None) or {}).get("responses_page")
    if isinstance(raw, dict):
        return raw
    return {"enabled": True} if raw is True else {}


def enabled(client) -> bool:
    return bool(config(client).get("enabled"))


def needs_reply_from(client) -> str:
    """The first answer date that is flagged ('' = every answer)."""
    d = str(config(client).get("needs_reply_from") or "").strip()[:10]
    try:
        date.fromisoformat(d)
    except ValueError:
        return ""
    return d


def answer_id(email: str, answered_at: str, q3_raw: str) -> str:
    """A stable id for one answer: the same three fields the page's read
    marker already keys on (respKey), hashed so it is safe in markup."""
    raw = "|".join(((email or "").strip().lower(), (answered_at or "").strip(),
                    (q3_raw or "").strip()))
    return "f" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def edit_key(email: str) -> str:
    """The `mid` an FTV reply's edit pair is saved under in
    member_reply_edits.csv (the answer has no member id; the email is the key
    the whole first-timer loop matches on)."""
    return (email or "").strip().lower()


def is_ftv_edit(row) -> bool:
    return str((row or {}).get("template_key") or "").startswith(survey_reply.TEMPLATE_PREFIX)


def _history(client, email: str, climber_id: str, outreach_rows, edits_by_key: dict,
             used: set) -> list:
    """Every live email on record for this person, newest first.
    item = {date, trigger, kind, label, template_key, title, variant, subject,
    body, source}; source = sent (exact words) | template | missing."""
    trig_template = {t.name: t.template_id for t in client.triggers}
    tag_template = {t.tag: t.template_id for t in client.triggers}
    out = []
    for row in outreach_rows or []:
        trig = (row.get("trigger_name") or "").strip()
        if not trig or trig in HISTORY_SKIP or (row.get("mode") or "").strip() == "test":
            continue
        em = (row.get("email") or "").strip().lower()
        cid = (row.get("climber_id") or "").strip()
        if not ((email and em == email) or (climber_id and cid == climber_id)):
            continue
        d = (row.get("sent_date") or "").strip()[:10]
        if not d:
            continue
        tag = (row.get("tag") or "").strip()
        item = {"date": d, "trigger": trig, "kind": "auto", "label": "",
                "template_key": "", "title": "", "variant": "", "subject": "",
                "body": "", "source": "template"}
        if trig == survey_reply.REPLY_TRIGGER:
            item.update(kind="reply", template_key=tag, title=survey_reply.title_of(tag),
                        label="Reply from staff")
            best = None
            for i, e in enumerate(edits_by_key.get(email, [])):
                key = (email, i)
                if key in used or (e.get("template_key") or "").strip() != tag:
                    continue
                gap = member_views._day_gap(e.get("ts") or "", d)
                if gap <= 1 and (best is None or gap < best[0]):
                    best = (gap, key, e)
            if best is not None:
                used.add(best[1])
                e = best[2]
                item["subject"] = e.get("edited_subject") or e.get("original_subject") or ""
                item["body"] = e.get("edited_body") or e.get("original_body") or ""
                item["source"] = "sent"
        elif trig in MEMBER_LABEL:
            item.update(label=MEMBER_LABEL[trig],
                        template_key=tag if trig != "member_survey" else "")
        else:
            item["template_key"] = trig_template.get(trig) or tag_template.get(tag) or ""
        tk = item["template_key"]
        if item["source"] == "template":
            if tk in client.templates:
                if client.templates[tk].subject_b:
                    item["variant"] = member_views._ab_variant(em or email, tk)
            else:
                item["source"] = "missing"
        out.append(item)
    out.sort(key=lambda x: (x["date"], x["kind"] == "reply"), reverse=True)
    return out


def _charts(rows: list, blocker_options: list) -> dict:
    """Month by month: how many answered, the average overall score (1 to 5)
    and the share who said climbing is likely to become a regular thing; plus
    the all-time count behind each main issue (zero bars included)."""
    by_month: dict = {}
    for r in rows:
        m = (r.get("answered_at") or "")[:7]
        if len(m) != 7:
            continue
        b = by_month.setdefault(m, {"n": 0, "scores": [], "intent": 0, "likely": 0})
        b["n"] += 1
        if r.get("overall") is not None:
            b["scores"].append(r["overall"])
        if r.get("intent"):
            b["intent"] += 1
            if r["intent"] == "high":
                b["likely"] += 1
    months = []
    for m in sorted(by_month):
        b = by_month[m]
        months.append({
            "month": m, "n": b["n"], "n_scored": len(b["scores"]),
            "avg_overall": (round(sum(b["scores"]) / len(b["scores"]), 2)
                            if b["scores"] else None),
            "n_intent": b["intent"], "likely": b["likely"],
            "likely_pct": (round(100.0 * b["likely"] / b["intent"], 1)
                           if b["intent"] else None),
        })
    issues = {o: 0 for o in (blocker_options or [])}
    none = 0
    for r in rows:
        if r.get("blocker"):
            issues[r["blocker"]] = issues.get(r["blocker"], 0) + 1
        else:
            none += 1
    scored = [r["overall"] for r in rows if r.get("overall") is not None]
    with_intent = [r for r in rows if r.get("intent")]
    return {
        "months": months,
        "issues": [[k, v] for k, v in sorted(issues.items(), key=lambda kv: (-kv[1], kv[0]))],
        "no_issue": none,
        "total": len(rows),
        "avg_overall": round(sum(scored) / len(scored), 2) if scored else None,
        "n_scored": len(scored),
        "likely": sum(1 for r in with_intent if r["intent"] == "high"),
        "n_intent": len(with_intent),
    }


def charts(survey_rows, blocker_options=None) -> dict:
    """The by-month chart numbers straight from the payload's
    survey.all_responses rows. Does not need the answer pages switched on, so
    both gyms' Insights get the same charts (Luke 2026-11-18)."""
    rows = []
    for r in survey_rows or []:
        row = dict(r)
        row["overall"] = survey_reply.overall_score(r.get("q1_overall"))
        rows.append(row)
    return _charts(rows, blocker_options)


def build(client, asof: date, *, survey_rows, outreach_rows, first_names=None,
          edit_rows=None, settings=None, blocker_options=None) -> dict:
    """Pure: inputs in, the `ftv_replies` payload block out.

    `survey_rows` are the payload's survey.all_responses dicts (engine.
    _survey_block), `first_names` {climber_id: first name} for the greeting,
    `edit_rows` the saved reply-edit pairs (member_reply_edits.csv)."""
    first_names = first_names or {}
    start = needs_reply_from(client)
    replies_all = survey_reply.replies_by_email(outreach_rows)
    edits_by_key: dict = {}
    for e in edit_rows or []:
        if is_ftv_edit(e):
            edits_by_key.setdefault((e.get("mid") or "").strip().lower(), []).append(e)
    for k in edits_by_key:
        edits_by_key[k].sort(key=lambda e: e.get("ts") or "")
    used: set = set()

    answered_by_email: dict = {}
    for r in survey_rows or []:
        em = (r.get("email") or "").strip().lower()
        if em:
            answered_by_email.setdefault(em, []).append((r.get("answered_at") or "")[:10])
    for k in answered_by_email:
        answered_by_email[k].sort()

    rows, seen = [], {}
    for r in survey_rows or []:
        em = (r.get("email") or "").strip().lower()
        answered = (r.get("answered_at") or "")[:10]
        rid = answer_id(em, r.get("answered_at") or "", r.get("q3_blocker_raw") or "")
        seen[rid] = seen.get(rid, 0) + 1
        if seen[rid] > 1:                      # two identical answers: keep ids unique
            rid = f"{rid}-{seen[rid]}"
        cid = (r.get("climber_id") or "").strip()
        later = [d for d in answered_by_email.get(em, []) if d > answered]
        mine = survey_reply.replies_for(replies_all.get(em, []), answered,
                                        later[0] if later else "")
        safety = bool(r.get("safety"))
        in_window = bool(answered) and (not start or answered >= start)
        can_reply = bool(em) and in_window and not safety
        sug = survey_reply.suggest(safety=safety, blocker=r.get("blocker"),
                                   q1_overall=r.get("q1_overall"))
        row = dict(r)
        row.update({
            "id": rid,
            "first_name": first_names.get(cid, "") if cid else "",
            "overall": survey_reply.overall_score(r.get("q1_overall")),
            "in_window": in_window,
            "can_reply": can_reply,
            "replies": mine,
            "replied": bool(mine),
            "reply_date": mine[-1]["date"] if mine else "",
            "needs_reply": can_reply and not mine,
            "suggestion": sug,
            "history": _history(client, em, cid, outreach_rows, edits_by_key, used),
        })
        rows.append(row)
    rows.sort(key=lambda x: x.get("answered_at") or "", reverse=True)

    from_email = manager = ""
    if settings is not None:
        from_email = getattr(settings, "sender_email", "") or ""
    manager = str((client.links or {}).get("manager_name", "") or "")
    return {
        "enabled": True,
        "as_of": asof.isoformat(),
        "needs_reply_from": start,
        "rows": rows,
        "needs_reply": sum(1 for x in rows if x["needs_reply"]),
        "templates": survey_reply.preview_templates(client),
        "timeline_templates": member_views.timeline_templates(client),
        "first_name_sentinel": survey_reply.FIRST_NAME_SENTINEL,
        "from_email": from_email,
        "manager_name": manager,
        "low_overall": survey_reply.LOW_OVERALL,
        "charts": _charts(rows, blocker_options),
    }
