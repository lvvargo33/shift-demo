"""Live front-desk check-in alert (Membership Surveys SPEC, 2026-08-21 late eve).

When a member who still has an OPEN survey follow-up checks in at the gym, email
the front-desk inbox once (per member, per local day) so staff can talk to them
while they are in the building. Luke's decisions 2026-08-21: email ONLY (no
dashboard banner), to hello@appalachianboulderingcenter.com, open follow-ups
only (status != resolved; promoters never alert). The morning digest was CUT the
same day, so this is the only proactive channel.

Where it runs: a background thread in live_server.py on the always-on web
service (see live_server._checkin_alert_loop). This module holds everything
that can be tested without a network: the config reader, the pure candidate
builder, the email render, and the send/record step with an injectable ESP.

Gates (ALL must hold, else off with a reason): member_survey.enabled,
member_survey.checkin_alert.enabled, live sending on THIS service
(NUDGE_LIVE_ENABLED + Brevo creds, the same test the Send buttons use), and an
RGP key (RGP_API_KEY on the web service; locally rgp_pull's dev-key fallback).

Dedupe + record: one outreach-log row per alert, trigger_name
`member_checkin_alert`, climber_id = mid, email = THE INBOX (never the member's
address, so nothing that reads member emails out of the log counts it as a
member email), tag = the check-in postDate. The poll skips any mid already
alerted today, re-reading the log, so a restart cannot re-alert. Plus one
follow-up note ("Checked in 3:55 PM; alert emailed to ...") so the card shows
the visit. The note is for people; the outreach row is the dedupe.

Stale feed: the lookback is bounded (max_age_minutes, default 90), so a nightly
catch-up dump of old check-ins never alerts. Freshness (newest check-in seen,
lag in minutes) is reported for the Settings view.

A failed email is NOT logged (it retries next poll), the same rule the survey
send and the replies follow.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import member_followups, member_reply, member_responses

TRIGGER_NAME = "member_checkin_alert"
DEFAULT_INBOX = ""                 # no default on purpose: a blank inbox = off
DEFAULT_POLL_MINUTES = 10
DEFAULT_MAX_AGE_MINUTES = 90
DEFAULT_DASHBOARD_URL = "https://appalachian-live-dash.onrender.com"
STALE_AFTER_MINUTES = 120          # Settings row warns past this lag
NOTE_BY = "check-in alert"
FACILITY_TZ = "America/New_York"   # RGP postDate is facility local time (ET)


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

@dataclass
class Config:
    enabled: bool = False
    inbox: str = DEFAULT_INBOX
    poll_minutes: int = DEFAULT_POLL_MINUTES
    max_age_minutes: int = DEFAULT_MAX_AGE_MINUTES
    dashboard_url: str = DEFAULT_DASHBOARD_URL

    def as_view(self) -> dict:
        return {"enabled": self.enabled, "inbox": self.inbox,
                "poll_minutes": self.poll_minutes,
                "max_age_minutes": self.max_age_minutes}


def load_config(client) -> Config:
    """Read `member_survey.checkin_alert` off a ClientConfig. No block = off."""
    ms = getattr(client, "member_survey", None) or {}
    raw = ms.get("checkin_alert") or {}
    if not isinstance(raw, dict):
        raw = {}

    def _int(k, d):
        try:
            v = int(raw.get(k, d) or d)
        except (TypeError, ValueError):
            v = d
        return v if v > 0 else d

    return Config(
        enabled=bool(raw.get("enabled", False)),
        inbox=str(raw.get("inbox", DEFAULT_INBOX) or "").strip().lower(),
        poll_minutes=_int("poll_minutes", DEFAULT_POLL_MINUTES),
        max_age_minutes=_int("max_age_minutes", DEFAULT_MAX_AGE_MINUTES),
        dashboard_url=str(raw.get("dashboard_url", DEFAULT_DASHBOARD_URL)
                          or DEFAULT_DASHBOARD_URL).strip(),
    )


def off_reasons(client, *, survey_enabled: bool, send_enabled: bool,
                rgp_key: bool) -> list[str]:
    """Why the alert is off, in plain words. Empty list = on."""
    cfg = load_config(client)
    out = []
    if not survey_enabled:
        out.append("member_survey.enabled is false")
    if not cfg.enabled:
        out.append("member_survey.checkin_alert.enabled is false")
    if not cfg.inbox:
        out.append("no inbox configured (member_survey.checkin_alert.inbox)")
    if not send_enabled:
        out.append("sending is not switched on for this server (NUDGE_LIVE_ENABLED + Brevo key)")
    if not rgp_key:
        out.append("RGP_API_KEY is not set on this server")
    return out


# --------------------------------------------------------------------------
# time helpers
# --------------------------------------------------------------------------

def facility_now() -> datetime:
    """Now in the facility's zone, naive (to compare with RGP postDate, which
    is naive facility-local). tzdata missing = DST-aware fixed-offset guess."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(FACILITY_TZ)).replace(tzinfo=None)
    except Exception:  # noqa: BLE001 - no tzdata on this box
        utc = datetime.now(timezone.utc).replace(tzinfo=None)
        return utc - timedelta(hours=4 if _is_us_dst(utc) else 5)


def _is_us_dst(d: datetime) -> bool:
    """Second Sunday of March .. first Sunday of November (US rule)."""
    y = d.year
    march = datetime(y, 3, 1)
    second_sun_march = march + timedelta(days=(6 - march.weekday()) % 7 + 7)
    nov = datetime(y, 11, 1)
    first_sun_nov = nov + timedelta(days=(6 - nov.weekday()) % 7)
    return second_sun_march <= d < first_sun_nov


def parse_post_date(s: str) -> datetime | None:
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None


def fmt_clock(d: datetime) -> str:
    """3:55 PM (no leading zero, no platform-specific strftime flags)."""
    h = d.hour % 12 or 12
    return f"{h}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"


# --------------------------------------------------------------------------
# RGP fetch (thin, injectable)
# --------------------------------------------------------------------------

def rgp_key_present() -> bool:
    """True when a real pull could authenticate: RGP_API_KEY in env, or (local
    dev only) rgp_pull's dev-key fallback resolves."""
    if (os.getenv("RGP_API_KEY") or "").strip():
        return True
    try:
        import rgp_pull  # top-level script; lazy so importing this module never needs it
        rgp_pull._api_key()
        return True
    except Exception:  # noqa: BLE001
        return False


def fetch_recent(since: datetime, until: datetime) -> list[dict]:
    """Check-ins between `since` and `until` (facility-local, naive) as
    [{customerGuid, postDate, status}]. Raises on an API error; the caller
    reports and skips the poll. customerDetails=false: names come from the
    member file, not the feed."""
    import rgp_pull  # lazy: the script loads .env and needs the key only on a call
    params = {"startDateTime": since.strftime("%Y-%m-%d %H:%M:%S"),
              "endDateTime": until.strftime("%Y-%m-%d %H:%M:%S"),
              "customerDetails": "false"}
    out = []
    for row in rgp_pull._paginate(f"/checkins/facility/{rgp_pull.FACILITY}",
                                  params=params, key="checkins", progress_every=0):
        ci = row.get("checkin", row) if isinstance(row, dict) else {}
        out.append({"customerGuid": str(ci.get("customerGuid") or "").strip(),
                    "postDate": str(ci.get("postDate") or ""),
                    "status": str(ci.get("status") or "")})
    return out


# --------------------------------------------------------------------------
# the pure builder
# --------------------------------------------------------------------------

@dataclass
class Result:
    candidates: list = field(default_factory=list)   # dicts, one per member to alert
    skipped: dict = field(default_factory=dict)      # reason -> count
    fetched: int = 0
    newest_checkin_at: str = ""                      # over ALL fetched rows
    feed_lag_minutes: int | None = None
    sent: list = field(default_factory=list)
    failed: list = field(default_factory=list)       # (candidate, error)
    rows: list = field(default_factory=list)         # outreach rows appended
    notes: int = 0                                   # follow-up notes appended

    def skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1


def open_followups(responses: list) -> dict:
    """mid -> response dict for every listed response that still wants a
    person: a detractor whose follow-up card is not resolved. The views already
    give a detractor with no CSV row a default `open` card. The list carries
    one row per SURVEY (Block 20 decision 18), so a member who answered twice
    is represented by their NEWEST survey."""
    out = {}
    for r in responses or []:
        fu = r.get("fu")
        if not r.get("is_detractor") or not fu:
            continue
        if (fu.get("status") or "") == "resolved":
            continue
        mid = str(r.get("mid") or "")
        prev = out.get(mid)
        if prev is None or str(r.get("answered_at") or "") > str(prev.get("answered_at") or ""):
            out[mid] = r
    return out


def alerted_today(outreach_rows, today: str) -> set:
    """mids with a `member_checkin_alert` row dated today (any mode but test)."""
    out = set()
    for r in outreach_rows or []:
        if (r.get("trigger_name") or "").strip() != TRIGGER_NAME:
            continue
        if (r.get("mode") or "").strip() == "test":
            continue
        if (r.get("sent_date") or "")[:10] == today:
            out.add((r.get("climber_id") or "").strip())
    return out


def build(responses: list, checkins: list, outreach_rows, today: str,
          now: datetime, max_age_minutes: int = DEFAULT_MAX_AGE_MINUTES) -> Result:
    """Who gets an alert from this poll. Sends nothing, writes nothing.

    `responses` = payload["members"]["responses"] (member_views.build),
    `checkins` = fetch_recent() rows, `now` = facility-local naive clock.
    One candidate per member (latest check-in wins)."""
    res = Result()
    res.fetched = len(checkins or [])
    wanted = open_followups(responses)
    done = alerted_today(outreach_rows, today)
    by_mid: dict = {}
    newest: datetime | None = None
    for ci in checkins or []:
        at = parse_post_date(ci.get("postDate", ""))
        if at is None:
            res.skip("bad_post_date")
            continue
        if newest is None or at > newest:
            newest = at
        age = (now - at).total_seconds() / 60.0
        mid = (ci.get("customerGuid") or "").strip()
        if not mid:
            res.skip("no_guid")
            continue
        if mid not in wanted:
            res.skip("not_flagged")
            continue
        if age > max_age_minutes:
            res.skip("too_old")
            continue
        if age < -15:                       # feed clock ahead of ours: distrust
            res.skip("future")
            continue
        if mid in done:
            res.skip("already_today")
            continue
        prev = by_mid.get(mid)
        if prev is None or at > prev["checkin_dt"]:
            r = wanted[mid]
            by_mid[mid] = {"mid": mid, "name": r.get("name") or mid,
                           "first_name": r.get("first_name") or "",
                           "checkin_at": at.strftime("%Y-%m-%d %H:%M:%S"),
                           "checkin_dt": at, "response": r}
    res.candidates = sorted(by_mid.values(), key=lambda c: (c["checkin_at"], c["mid"]))
    if newest is not None:
        res.newest_checkin_at = newest.strftime("%Y-%m-%d %H:%M:%S")
        res.feed_lag_minutes = max(0, int((now - newest).total_seconds() // 60))
    return res


# --------------------------------------------------------------------------
# the email
# --------------------------------------------------------------------------

def _q(s: str) -> str:
    s = (s or "").strip()
    return "" if s in ("", "-") else s


def render_email(cand: dict, dashboard_url: str = DEFAULT_DASHBOARD_URL) -> tuple[str, str]:
    """(subject, body). Body is plain paragraphs separated by blank lines; the
    Brevo shell turns them into <p>s and the one markdown link into an anchor.
    Staff-facing: name, their scores, their own words, the follow-up state,
    the suggested reply and the staff notes. Nothing else about the member."""
    r = cand["response"]
    sc = r.get("scores") or {}
    fu = r.get("fu") or {}
    rec = r.get("recommend") or {}
    when = fmt_clock(cand["checkin_dt"]) if cand.get("checkin_dt") else cand.get("checkin_at", "")
    name = cand.get("name") or "A member"
    subject = f"Heads up: {name} just checked in (open follow-up)"

    paras = [f"{name} checked in at {when} today and still has an open survey follow-up. "
             f"If someone can, say hello while they are here."]

    bits = []
    if sc.get("nps") is not None:
        bits.append(f"recommend {sc['nps']}/10")
    if sc.get("remain") is not None:
        bits.append(f"stay 6 months {sc['remain']}/10")
    lowest = ""
    if rec.get("metric"):
        lab = rec.get("metric_label") or member_responses.METRIC_LABEL.get(rec["metric"], rec["metric"])
        val = sc.get(rec["metric"])
        lowest = f" Lowest: {lab} {val}/10." if val is not None else f" Lowest: {lab}."
    if bits or lowest:
        paras.append("Scores: " + ", ".join(bits) + "." + lowest if bits else "Scores:" + lowest)

    txt = r.get("text") or {}
    why, improve = _q(txt.get("why")), _q(txt.get("improve"))
    if why:
        paras.append(f'In their words: "{why}"')
    if improve:
        paras.append(f'What they would improve: "{improve}"')

    status = member_followups.STATUS_LABEL.get(fu.get("status") or "", fu.get("status") or "open")
    fparts = [f"Follow-up: {status}"]
    if fu.get("assignee"):
        fparts.append(f"assigned to {fu['assignee']}")
    if fu.get("due_date"):
        fparts.append(f"due {fu['due_date']}")
    line = ", ".join(fparts) + "."
    if fu.get("overdue"):
        line += " OVERDUE."
    if r.get("replied"):
        line += " A reply email already went out."
    paras.append(line)

    if rec.get("template_key"):
        paras.append(f"Suggested reply template: {member_reply.title_of(rec['template_key'])}.")
    guide = [g for g in (r.get("guidance") or []) if str(g).strip()]
    if guide:
        paras.append("Staff notes: " + " ".join(f"({i}) {g}" for i, g in enumerate(guide, 1)))

    url = (dashboard_url or DEFAULT_DASHBOARD_URL).strip()
    paras.append("To reply or update the follow-up, open the dashboard, then Members > "
                 f"Survey Responses: [{url}]({url})")
    paras.append("You get at most one of these per member per day, and none once the "
                 "follow-up is marked resolved.")
    return subject, "\n\n".join(paras)


# --------------------------------------------------------------------------
# send + record
# --------------------------------------------------------------------------

def log_row(mid: str, inbox: str, checkin_at: str, today: str, mode: str = "live") -> dict:
    """One outreach-log row. email = the INBOX on purpose (see module doc)."""
    return {"sent_date": today, "email": (inbox or "").strip().lower(),
            "climber_id": mid, "trigger_name": TRIGGER_NAME,
            "tag": checkin_at, "mode": mode}


def send_all(mc, client, res: Result, cfg: Config, today: str,
             dry_run: bool = False) -> Result:
    """Email every candidate to cfg.inbox through `mc.send_internal`, then
    record: one outreach row (dedupe) + one follow-up note (for people). A
    failed send records NOTHING, so the next poll retries. Dry run touches
    nothing. The caller appends `res.rows` to the outreach log and pushes."""
    from . import outreach
    for c in res.candidates:
        subject, body = render_email(c, cfg.dashboard_url)
        if dry_run:
            res.sent.append(c)
            continue
        try:
            mc.send_internal(cfg.inbox, subject, body, tag=TRIGGER_NAME)
        except Exception as e:                                 # noqa: BLE001
            res.failed.append((c, f"{type(e).__name__}: {e}"))
            continue
        res.sent.append(c)
        row = log_row(c["mid"], cfg.inbox, c["checkin_at"], today)
        res.rows.append(row)
        outreach.append(client, [row])
        try:
            when = fmt_clock(c["checkin_dt"]) if c.get("checkin_dt") else c["checkin_at"]
            member_followups.record(client, c["mid"],
                                    note=f"Checked in {when}; alert emailed to {cfg.inbox}",
                                    by=NOTE_BY)
            res.notes += 1
        except Exception as e:                                 # noqa: BLE001
            print(f"  (check-in alert: follow-up note not recorded for {c['mid']}: {e})")
    return res


def report_lines(res: Result, reasons: list[str] | None = None) -> list[str]:
    """Human lines for the server log / one-shot output."""
    if reasons:
        return ["  check-in alert: off (" + "; ".join(reasons) + ")"]
    lag = "feed lag unknown" if res.feed_lag_minutes is None else f"feed ~{res.feed_lag_minutes} min behind"
    out = [f"  check-in alert: {res.fetched} check-in(s) in window, "
           f"{len(res.candidates)} to alert, {lag}"
           + (f", newest {res.newest_checkin_at}" if res.newest_checkin_at else "")]
    if res.skipped:
        out.append("    skipped: " + ", ".join(f"{v} {k}" for k, v in sorted(res.skipped.items())))
    for c in res.sent:
        out.append(f"    alert {c['name']} ({c['mid'][:8]}...) checked in {c['checkin_at']}")
    for c, err in res.failed:
        out.append(f"    FAIL {c['name']}: {err}")
    return out
