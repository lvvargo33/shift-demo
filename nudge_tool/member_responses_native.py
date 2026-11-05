"""Member survey answers from Firestore, shaped for ABC's Members pages
(A2 step 4, built 2026-11-02; Luke: "ABC parity").

SHIFT's members answer Chris's shift_v3 instrument on /member-survey; each
answer is a Firestore session (survey_type "member") plus its invitation
message (customer_id = Beta climber_key, contact_email, member_context).
This module turns those into the same member_responses.MemberResponse /
ReadResult that ABC's SurveyMonkey parser produces, so member_views,
member_reply and the dashboard's Members views work unchanged. Reached only
when client.json member_survey.source == "native" (ABC has no such key).

Mapping (shift_v3 question id -> ABC field / metric key):
  core_nps -> nps, core_remain_6m -> remain, core_nps_why -> open_comment,
  experience_improve -> improve_comment, staff_recognition -> staff_shout_out
  selection metrics: core_staff_friendliness staff, core_community community,
  core_route_setting routes, core_cleanliness cleanliness, core_value value
  diagnostics: experience_staff_help staff_help, experience_info_access
  info_access, experience_safety safety, experience_access_climbs crowding,
  bouldering_quantity grade_range, bouldering_reset_frequency reset_frequency,
  training_overall training_area
Everything else (section ratings, section comments, circuits, sections
used, the member's inactive flag) goes in MemberResponse.extra, which the
response page lists under "More from this member".

Which sessions count: any member session with at least one answered
question (a partial one too, the way SurveyMonkey exports a partial row and
ABC shows it). Test customers (native_survey.TEST_CUSTOMER_PREFIX) never
show. Times are converted to the gym's local time BEFORE answered_at slices
the date (a 9 PM answer stays on its own day).

Reads are cached for CACHE_SECONDS per gym (the dashboard renders on every
page load; a full scan each time would be slow), and the reply route asks
for fresh=True so it never answers from a stale read.
"""
from __future__ import annotations

import csv
import time
from datetime import datetime, timezone
from pathlib import Path

from . import member_responses as mr
from . import native_survey as ns

CACHE_SECONDS = 60
_cache: dict = {}

SCALARS = {"core_nps": "nps", "core_remain_6m": "remain"}
TEXTS = {"core_nps_why": "open_comment", "experience_improve": "improve_comment",
         "staff_recognition": "staff_shout_out"}
METRICS = {
    "core_staff_friendliness": "staff", "core_community": "community",
    "core_route_setting": "routes", "core_cleanliness": "cleanliness",
    "core_value": "value",
    "experience_staff_help": "staff_help", "experience_info_access": "info_access",
    "experience_safety": "safety", "experience_access_climbs": "crowding",
    "bouldering_quantity": "grade_range", "bouldering_reset_frequency": "reset_frequency",
    "training_overall": "training_area",
}


def _local_iso(iso_utc, tz_name: str) -> str:
    """'YYYY-MM-DD HH:MM:SS' in the gym's zone ("" for nothing)."""
    s = ns._s(iso_utc).strip()
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s[:19]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        dt = dt.astimezone(ZoneInfo(tz_name or "UTC"))
    except Exception:  # noqa: BLE001
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _docs(client, db, fresh: bool):
    gid = ns.gym_id(client)
    hit = _cache.get(gid)
    if hit and not fresh and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1], hit[2]
    if db is None:
        db = ns._db()
    root = db.collection("gyms").document(gid)
    sessions = [s.to_dict() or {} for s in ns._retry(
        lambda: list(root.collection("sessions").stream()), ns._RETRY_DELAYS_PULL)]
    messages = [m.to_dict() or {} for m in ns._retry(
        lambda: list(root.collection("messages").stream()), ns._RETRY_DELAYS_PULL)]
    sessions = [s for s in sessions if s.get("gym_id") == gid and s.get("survey_type") == "member"]
    _cache[gid] = (time.monotonic(), sessions, messages)
    return sessions, messages


def _labels(session: dict, wording=None) -> tuple[dict, dict]:
    """(question id -> "Section: wording", option labels per question) from the
    session's own pinned instrument. `wording` = the page's display swap
    (MemberExperienceConfig.wording: "Appalachian Bouldering Center" and
    "at ABC " become the gym's names), so staff read what the member read."""
    from .survey_page.member_assets import ASSETS, MemberExperienceConfig
    wording = wording or MemberExperienceConfig().wording
    assets = ASSETS.get((session.get("definition_id"), session.get("definition_version")))
    if not assets:
        return {}, {}
    definition, presentation, _, _ = assets
    section = {}
    for page in presentation.pages:
        for g in page.groups:
            for qid in g.questions:
                section[qid] = page.title if page.module else ""
    words, opts = {}, {}
    for q in definition.questions:
        w = wording(q.wording)
        words[q.id] = f"{section.get(q.id)}: {w}" if section.get(q.id) else w
        opts[q.id] = dict(q.options)
    return words, opts


def _is_qr(session: dict) -> bool:
    """A QR-sign member survey (SHIFT member_qr.CYCLE_PREFIX; emailed ones are
    "m"+date). Inlined so this shared module needs no SHIFT-only import."""
    return str(session.get("cycle_id") or "").startswith("q")


def _response(session: dict, message: dict, tz: str, wording=None):
    answers = ((session.get("completion") or {}).get("answers")
               if session.get("state") == "completed" else None) or session.get("answers") or {}
    vals = {k: v.get("value") for k, v in answers.items()
            if isinstance(v, dict) and v.get("status") == "answered"}
    if not vals:
        return None, set()
    mid = str(message.get("customer_id") or "").strip()
    r = mr.MemberResponse(
        mid=mid, respondent_id=str(session.get("id") or ""),
        started_at=_local_iso(session.get("started_at"), tz),
        ended_at=_local_iso(session.get("completed_at") or session.get("last_saved_at"), tz))
    for qid, f in SCALARS.items():
        if isinstance(vals.get(qid), int):
            setattr(r, f, vals[qid])
    for qid, f in TEXTS.items():
        if isinstance(vals.get(qid), str):
            setattr(r, f, vals[qid].strip())
    for qid, k in METRICS.items():
        if isinstance(vals.get(qid), int):
            r.metrics[k] = vals[qid]
    words, opts = _labels(session, wording)
    used = set(SCALARS) | set(TEXTS) | set(METRICS)
    scores, comments, picks = [], [], []
    for qid in words:  # instrument order
        v = vals.get(qid)
        if qid in used or v is None:
            continue
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            scores.append([words[qid], v])
        elif isinstance(v, str) and v.strip():
            if v in opts.get(qid, {}):
                picks.append([words[qid], opts[qid][v]])
            else:
                comments.append([words[qid], v.strip()])
        elif isinstance(v, list) and v:
            picks.append([words[qid], ", ".join(opts.get(qid, {}).get(x, x) for x in v)])
    unknown = {qid for qid in vals if qid not in words and qid not in used}
    r.extra = {"context": dict(message.get("member_context") or {}),
               "picks": picks, "scores": scores, "comments": comments,
               "state": session.get("state") or "",
               # SHIFT QR sign (member_qr, 2026-11-05): its sessions' cycle id is "q"+date
               "channel": "qr" if _is_qr(session) else "email"}
    return r, unknown


def read(client, outreach_rows=None, *, db=None, fresh: bool = False) -> mr.ReadResult:
    """Every answered member session -> a ReadResult (see module doc). Raises
    on a Firestore failure (member_views.build_live reports it as sheet_ok
    false, the page says the answers could not be read)."""
    if outreach_rows is None:
        from . import outreach
        try:
            outreach_rows = outreach.load_rows(client)
        except Exception:  # noqa: BLE001
            outreach_rows = []
    tz = (ns.native_cfg(client).get("timezone") or "UTC")
    try:
        from .survey_page.member_routes import experience_config
        wording = experience_config(client).wording
    except Exception:  # noqa: BLE001 (a bad display block never hides answers)
        wording = None
    sessions, messages = _docs(client, db, fresh)
    msg_by_session: dict = {}
    for m in messages:
        sid = m.get("session_id")
        if sid and (sid not in msg_by_session
                    or ns._s(m.get("planned_at")) > ns._s(msg_by_session[sid].get("planned_at"))):
            msg_by_session[sid] = m
    res = mr.ReadResult()
    sends = mr.survey_sends(outreach_rows)
    parsed = []
    for s in sessions:
        m = msg_by_session.get(s.get("id")) or {}
        if str(m.get("customer_id") or "").startswith(ns.TEST_CUSTOMER_PREFIX):
            continue
        try:
            r, unknown = _response(s, m, tz, wording)
        except Exception:  # noqa: BLE001 (one odd document never hides the rest)
            continue
        if r is None:
            continue
        res.total_rows += 1
        res.unknown_row_labels |= unknown
        if not r.mid:
            res.blank_mid += 1
            continue
        parsed.append(r)
    # Rounds: each emailed send AND each QR answer is one survey occasion, so
    # a QR answer and a later emailed one are rounds 1 and 2, never one
    # "duplicate" (QR sign, 2026-11-05; the 90-day rule keeps them apart).
    occasions: dict = {}
    for r in parsed:
        if (r.extra or {}).get("channel") == "qr" and r.answered_at:
            occasions.setdefault(r.mid, []).append(r.answered_at[:10])
    for r in parsed:
        r.round_no = mr.round_of(sorted((sends.get(r.mid) or []) + occasions.get(r.mid, [])),
                                 r.answered_at)
    parsed.sort(key=lambda x: (x.ended_at or x.started_at or "", x.respondent_id))
    latest: dict = {}
    for r in parsed:
        key = (r.mid, r.round_no)
        if key in latest:
            res.duplicates_dropped += 1
        latest[key] = r
    res.responses = list(latest.values())
    return res


def member_index(client) -> dict:
    """climber_key -> {first_name, last_name, email, status, membership_start}
    for EVERY person on Beta's Memberships.csv (any status, youth too), so a
    member who answered and then cancelled still shows a name. status "ok" =
    an open membership today (the Members page calls that an active member),
    else "ended"; membership_start = their latest episode's start."""
    from datetime import date
    from . import gym_metrics
    path = Path(client.memberships_csv)
    if not path.exists():
        return {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    eps, _ = gym_metrics._beta_episodes(rows, date.today())
    latest: dict = {}
    for e in eps:
        if e["cid"] not in latest or e["start"] > latest[e["cid"]]["start"]:
            latest[e["cid"]] = e
    out: dict = {}
    for r in sorted(rows, key=lambda r: str(r.get("created_date") or "")):
        cid = str(r.get("climber_key") or "").strip()
        if not cid:
            continue
        e = latest.get(cid)
        out[cid] = {
            "first_name": str(r.get("first") or "").strip(),
            "last_name": str(r.get("last") or "").strip(),
            "email": str(r.get("email") or "").strip().lower(),
            "status": "ok" if e is not None and e["end"] is None else "ended",
            "membership_start": e["start"].isoformat() if e is not None else "",
        }
    return out
