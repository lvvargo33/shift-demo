"""SHIFT emailed member survey: who gets asked today (A2, built 2026-11-02).

Rules = the A2 interview (ROADMAP, 2026-10-23) + the 2026-10-28 sampling
ruling. Pure: no network, no writes. The send itself goes through
survey_dispatch (Brevo, program "member") and survey_mail (the
send_it_member_survey email, a link to /member-survey on Chris's shift_v3
questions).

Who counts as a member (the same plan-row rules as the day-14 check-in, so
the two cannot disagree; gym_metrics._beta_episodes underneath):
  * an open membership EPISODE on the run date (a plan switch within
    SWITCH_GAP days is the same episode), adult plans only (youth rows and
    kids discount codes out first), staff plans + freeze-fee rows out
  * 30+ days in a row as a member (decision 2), counted from the episode start
Held back (decision 4): frozen, staff (email list + staff plan rows), no
email, Mailchimp unsubscribed / cleaned, an email shared by two or more
current members (decision: shared or missing emails are skipped). The Shift
survey opt-out list and Brevo blocks are applied at send time by
survey_dispatch. Members with no check-in for `inactive_days` (60) or more
are INCLUDED and marked inactive (decision 4, revised).

How many a day (decision 2026-10-28, Chris's allocator rule): about 1/90 of
the eligible members every day, as a running remainder with no stored state:
    quota(day d) = floor(N*(d+1)/S) - floor(N*d/S)
with N = today's eligible members, S = spread_days (90), d = days since
go_live. Over S days that adds to N. Who first: never asked, then longest
since their last survey, then longest a member. A member surveyed within
cooldown_days (90) waits (decision 1; keyed on the permanent trigger name
`member_survey`, so renaming it resets everyone's clock). No reminder
(decision 5).

First wave (Chris 2026-11-03, Luke OK 2026-11-05): `first_wave_per_day`
(0 = off). The first-wave group = members who were already eligible on
go_live day (30+ days a member by then) and have never been asked. While any
of them is due, the day's quota is at least min(first_wave_per_day, how many
of them are left). It ends by itself when that group is done: anyone who
joins later was not eligible on go_live, so they follow the 1/90 share.

Switched OFF until `member_survey.enabled` is the JSON value true AND
`go_live` is a YYYY-MM-DD date (same switch shape as the day-14 check-in);
off = the report previews who WOULD be asked, nobody is.

Preview on local files (no network, nothing written):
    py -m nudge_tool.member_survey_native --memberships <csv> --sessions <csv>
       [--log <outreach_log.csv>] [--as-of YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from . import gym_metrics, member_checkin
from .livesend import SendTarget

TRIGGER_NAME = "member_survey"
CONFIG_KEY = "member_survey"
DEFAULTS = {"enabled": False, "go_live": "", "tag": "send_it_member_survey",
            "cooldown_days": 90, "min_member_days": 30, "inactive_days": 60,
            "spread_days": 90, "first_wave_per_day": 0}
_INTS = ("cooldown_days", "min_member_days", "inactive_days", "spread_days")
FIRST_WAVE_MAX = 50     # a typo like 500 cannot email the whole gym in a day


def load_cfg(client) -> dict:
    """client.json `member_survey` over DEFAULTS. Never raises: anything off
    leaves the survey OFF and says why in cfg["_problem"]."""
    cfg = dict(DEFAULTS)
    try:
        raw = json.loads((Path(client.client_dir) / "client.json").read_text(encoding="utf-8"))
        blk = raw.get(CONFIG_KEY) if isinstance(raw, dict) else None
    except (OSError, ValueError) as exc:
        cfg["_problem"] = f"client.json unreadable ({type(exc).__name__})"
        return cfg
    if blk is None:
        return cfg
    if not isinstance(blk, dict):
        cfg["_problem"] = f"`{CONFIG_KEY}` is not an object; left OFF"
        return cfg
    cfg.update({k: v for k, v in blk.items() if not str(k).startswith("_")})

    def off(why: str) -> None:
        cfg["_problem"] = f"{why}; left OFF"
        cfg["enabled"] = False

    for k in _INTS:
        v = cfg[k]
        if type(v) is not int or not 1 <= v <= 3650:
            cfg[k] = DEFAULTS[k]
            off(f"`{k}` = {blk.get(k)!r} must be a whole number of days")
    fw = cfg.get("first_wave_per_day")
    if type(fw) is not int or not 0 <= fw <= FIRST_WAVE_MAX:
        cfg["first_wave_per_day"] = DEFAULTS["first_wave_per_day"]
        off(f"`first_wave_per_day` = {blk.get('first_wave_per_day')!r} must be a whole number"
            f" from 0 to {FIRST_WAVE_MAX}")
    if cfg.get("enabled") not in (True, False):
        off(f"`enabled` = {blk.get('enabled')!r} must be true or false (no quotes)")
    if str(cfg.get("go_live") or "").strip() and member_checkin.go_live_date(cfg) is None:
        off(f"`go_live` = {blk.get('go_live')!r} is not a YYYY-MM-DD date")
    if not str(cfg.get("tag") or "").startswith("send_it_"):
        off(f"`tag` = {blk.get('tag')!r} must start with send_it_")
    return cfg


def is_live(cfg: dict) -> bool:
    return member_checkin.is_live(cfg)


@dataclass
class Member:
    climber_id: str
    name: str
    first_name: str
    email: str
    since: date                  # episode start
    plan: str
    last_checkin: date | None
    days_since_checkin: int | None
    inactive: bool
    last_survey: str = ""        # last member_survey send date ("" = never)
    skip: str = ""

    @property
    def context(self) -> dict:
        """What the reply screen shows next to the answer (stored on the
        survey message at send time)."""
        return {"inactive": self.inactive, "days_since_checkin": self.days_since_checkin,
                "member_since": self.since.isoformat(), "plan": self.plan[:120]}


@dataclass
class Result:
    eligible: list = field(default_factory=list)   # pass every screen (N)
    picked: list = field(default_factory=list)     # asked today (preview: the first `quota` of due)
    due: list = field(default_factory=list)        # every member due, in send order (the
                                                   # send run walks this until `quota` emails
                                                   # actually go out; fresh-eyes 11/02)
    waiting: int = 0                               # eligible, not due or not picked
    cooling: int = 0                               # surveyed inside cooldown_days
    too_new: int = 0                               # member < min_member_days
    held: list = field(default_factory=list)       # Member with .skip
    quota: int = 0
    share: int = 0                                 # the plain 1/spread_days quota
    first_wave_left: int = 0                       # due first-wave members (see module doc)


def last_checkins(sessions) -> dict[str, date]:
    """climber_key -> last check-in date, from a Sessions.csv path or rows."""
    rows = sessions
    if isinstance(sessions, (str, Path)):
        p = Path(sessions)
        if not p.exists():
            return {}
        with open(p, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    out: dict[str, date] = {}
    for r in rows or []:
        cid = str(r.get("climber_key") or "").strip()
        d = gym_metrics._d(r.get("entry"))
        if cid and d is not None and (cid not in out or d > out[cid]):
            out[cid] = d
    return out


def quota_for(n: int, asof: date, anchor: date | None, spread: int) -> int:
    """Running-remainder share of n for the run on asof (see module doc)."""
    if n <= 0:
        return 0
    d = max(0, (asof - anchor).days) if anchor is not None else 0
    return (n * (d + 1)) // spread - (n * d) // spread


def is_first_wave(m: Member, go_live: date, min_member_days: int) -> bool:
    """Never asked, and already min_member_days a member on go_live day."""
    return not m.last_survey and (go_live - m.since).days >= min_member_days


def find(memberships, sessions, asof: date, cfg: dict, *, log=None, unsubscribed=(),
         staff_emails=(), skip_emails=()) -> Result:
    """Who gets the member survey on the run dated asof. `skip_emails` =
    inboxes getting another email from today's run (they wait a day)."""
    rows = [r for r in gym_metrics._rows(memberships) if not member_checkin._is_youth(r)]
    by_cid: dict[str, list] = {}
    for r in rows:
        cid = str(r.get("climber_key") or "").strip()
        if cid:
            by_cid.setdefault(cid, []).append(r)
    eps, _ = gym_metrics._beta_episodes(rows, asof)
    seen_ci = last_checkins(sessions)
    # days since check-in are counted from the check-in file's own last day
    # (not the run date), so a stale file cannot mark everyone inactive and
    # save that on their survey (fresh-eyes 11/02: 262 of 262 on stale files)
    ci_ref = min(asof, max(seen_ci.values())) if seen_ci else asof
    unsub = {e.strip().lower() for e in unsubscribed if e}
    staff = {e.strip().lower() for e in staff_emails if e}
    today = {e.strip().lower() for e in skip_emails if e}
    min_days, inactive_days = int(cfg["min_member_days"]), int(cfg["inactive_days"])
    res = Result()

    current: list[Member] = []
    for e in eps:
        if e["end"] is not None:
            continue
        mine = by_cid.get(e["cid"], [])
        plans = [r for r in mine if not member_checkin._is_freeze_fee(r)
                 and (gym_metrics._d(r.get("created_date")) or asof) <= asof]
        held_now = [r for r in plans if member_checkin._running(r, asof)]
        show = max(held_now or plans or [{}], key=lambda r: str(r.get("created_date") or ""))
        last = seen_ci.get(e["cid"])
        days = max(0, (ci_ref - last).days) if last is not None else None
        m = Member(e["cid"],
                   " ".join(x for x in (str(show.get("first") or "").strip(),
                                        str(show.get("last") or "").strip()) if x),
                   str(show.get("first") or "").strip(), str(show.get("email") or "").strip(),
                   e["start"], str(show.get("prices") or "").strip(), last, days,
                   days is None or days >= inactive_days)
        frozen = any(member_checkin._is_freeze_fee(r) and member_checkin._running(r, asof)
                     and (gym_metrics._d(r.get("created_date")) or asof) >= e["start"]
                     for r in mine) or \
            bool(e["open_rows"]) and all(r["frozen"] for r in e["open_rows"])
        key = m.email.lower()
        if frozen:
            m.skip = "frozen"
        elif not m.email:
            m.skip = "no email"
        elif key in staff:
            m.skip = "staff"
        elif key in unsub:
            m.skip = "unsubscribed in Mailchimp"
        current.append(m)

    owners: dict[str, set] = {}
    for m in current:
        if m.email:
            owners.setdefault(m.email.lower(), set()).add(m.climber_id)
    for m in current:
        if not m.skip and len(owners.get(m.email.lower(), ())) > 1:
            m.skip = "email shared with another member"
        if m.skip:
            res.held.append(m)
            continue
        if (asof - m.since).days < min_days:
            res.too_new += 1
            continue
        res.eligible.append(m)

    cooldown = timedelta(days=int(cfg["cooldown_days"]))
    due: list[Member] = []
    for m in res.eligible:
        dates = sorted(log.sent_dates(m.email, TRIGGER_NAME)) if log is not None else []
        m.last_survey = dates[-1][:10] if dates else ""
        if m.last_survey and asof - date.fromisoformat(m.last_survey) < cooldown:
            res.cooling += 1
            continue
        if m.email.lower() in today:
            continue
        due.append(m)
    due.sort(key=lambda m: (m.last_survey != "", m.last_survey, m.since, m.climber_id))
    anchor = member_checkin.go_live_date(cfg)
    res.share = quota_for(len(res.eligible), asof, anchor, int(cfg["spread_days"]))
    res.quota = res.share
    fw = int(cfg.get("first_wave_per_day") or 0)
    if fw > 0 and anchor is not None:
        res.first_wave_left = sum(1 for m in due if is_first_wave(m, anchor, min_days))
        res.quota = max(res.share, min(fw, res.first_wave_left))
    res.due = due
    res.picked = due[:res.quota]
    res.waiting = len(res.eligible) - len(res.picked)
    return res


@dataclass
class MemberTarget(SendTarget):
    """A send target for survey_dispatch (route "member"). cycle_id names this
    member's survey session; ledger_key makes the send ledger per cycle;
    member_context rides on the Firestore message for the reply screen."""
    cycle_id: str = ""
    ledger_key: str = ""
    ledger_window_days: int = 0      # >0: a ledger entry older than this is spent
    member_context: dict | None = None


def targets(res: Result, cfg: dict, asof: date, *, every_due: bool = False) -> list[MemberTarget]:
    from .native_survey import member_cycle_id
    cyc = member_cycle_id(asof)
    tag = str(cfg.get("tag") or DEFAULTS["tag"])
    return [MemberTarget(m.climber_id, m.name, m.first_name, m.email, tag, tag, TRIGGER_NAME,
                         anchor_date=m.since.isoformat(), cycle_id=cyc,
                         # ONE ledger entry per member, good for cooldown_days: a
                         # send whose log row was lost, or a Brevo timeout, is
                         # never re-sent the next day (fresh-eyes 11/02)
                         ledger_key=TRIGGER_NAME,
                         ledger_window_days=int(cfg.get("cooldown_days") or DEFAULTS["cooldown_days"]),
                         member_context=m.context)
            for m in (res.due if every_due else res.picked)]


def report_lines(res: Result, cfg: dict, live: bool, asof: date | None = None) -> list[str]:
    gl = member_checkin.go_live_date(cfg)
    if live and asof is not None and gl is not None and asof < gl:
        state = f"ON from {gl} (nobody asked before then)"
    elif live:
        state = "LIVE"
    else:
        state = "OFF (preview only: who WOULD be asked today, nobody is)"
    lines = [f"  member survey: {state}, go_live={cfg.get('go_live') or '(not set)'}"]
    if cfg.get("_problem"):
        lines.append(f"    CONFIG PROBLEM: {cfg['_problem']}")
    inactive = sum(1 for m in res.eligible if m.inactive)
    if int(cfg.get("first_wave_per_day") or 0) > 0:
        how = (f"first wave {cfg['first_wave_per_day']} a day, {res.first_wave_left} left"
               if res.first_wave_left else f"first wave done, 1/{cfg['spread_days']} share")
    else:
        how = f"1/{cfg['spread_days']} share"
    lines.append(f"    eligible {len(res.eligible)} (inactive {inactive}), asked today {len(res.picked)}"
                 f" ({how}), surveyed in the last {cfg['cooldown_days']} days"
                 f" {res.cooling}, under {cfg['min_member_days']} days a member {res.too_new},"
                 f" held back {len(res.held)}")
    for m in res.picked:
        lines.append(f"    {'ask ' if live else 'would'} {m.name:<22.22} {m.email}  (member since "
                     f"{m.since}{', INACTIVE' if m.inactive else ''}"
                     f"{', last survey ' + m.last_survey if m.last_survey else ', never asked'})")
    reasons: dict[str, int] = {}
    for m in res.held:
        reasons[m.skip] = reasons.get(m.skip, 0) + 1
    if reasons:
        lines.append("    held back: " + ", ".join(f"{k} {v}" for k, v in sorted(reasons.items())))
    return lines


def main() -> None:
    ap = argparse.ArgumentParser(description="Preview the member survey picker (no network).")
    ap.add_argument("--memberships", required=True)
    ap.add_argument("--sessions", required=True)
    ap.add_argument("--log", default=None, help="an outreach_log.csv (default: no send history)")
    ap.add_argument("--as-of", default=None)
    a = ap.parse_args()
    asof = date.fromisoformat(a.as_of) if a.as_of else date.today()
    log = None
    if a.log:
        from .outreach import OutreachLog
        log = OutreachLog()
        with open(a.log, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                em = (r.get("email") or "").strip().lower()
                name = (r.get("trigger_name") or "").strip() or (r.get("tag") or "").strip()
                sent = (r.get("sent_date") or "").strip()[:10]
                if em and name and sent:
                    log.by_email_trigger.setdefault((em, name), []).append(sent)
    cfg = dict(DEFAULTS)
    res = find(a.memberships, a.sessions, asof, cfg, log=log)
    for line in report_lines(res, cfg, live=False, asof=asof):
        print(line)


if __name__ == "__main__":
    main()
