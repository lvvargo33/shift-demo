"""Day-14 member check-in tagger (ROADMAP Block 25 Part B, 2026-10-26).

Every new SHIFT member gets one check-in email about 14 days after they join.
We only TAG: the daily send run adds `send_it_member_check_in` in Mailchimp
and Chris's Customer Journey on that tag sends the email (decision 2026-10-21:
day-14 stays on Mailchimp, "a retention campaign, to be safe"). Copy = Chris's
draft in the Shift tone doc, which lives in his journey, not here.

Who (Block 25 decisions 2, 9, 10 + Part B build list):
  * "new member" = the start of a membership EPISODE: a priced plan after a
    gap with none (the first one ever counts). A plan switch (one plan ends,
    another starts within SWITCH_GAP days) is the same episode, so it never
    triggers. Same chaining as the retention study and the Home "Gym" card
    (gym_metrics._beta_episodes, reused here so the three cannot disagree).
  * adult plans only: youth rows are left out BEFORE episodes are built (the
    email would reach a parent). Youth = the plan name, or a kids discount
    code on the row (COREKIDS: kids on an adult plan). So a parent who paid
    for a kid's plan on their own account and later joins counts as new, and
    so does a teen who moves from a youth plan to an adult one (fresh-eyes
    round 2, 2026-10-26: an episode that STARTED on a youth plan used to be
    skipped, which missed the parent). Staff plans + freeze-fee rows are left
    out by _beta_episodes itself; a staff member who moves to a paid plan is
    caught by the staff email list.
  * due on the first run on or after episode start + `day` days, and only if
    that day is on or after `go_live` (no catch-up for people already past
    day 14 when we switch on). A run that is more than `late_days` past the
    due day does not send (a member frozen on day 14 who unfreezes on day 60
    should not get a "two weeks in" email).
  * held back when: the episode has ended, the member is frozen, every plan
    they hold has a cancel date set (they already asked to leave), no email,
    Mailchimp unsubscribed/cleaned, staff, the spacing guard
    (min_days_between_sends), or this inbox already got a check-in on/after
    the episode start (once per episode; a rejoiner can get it again).

Frozen: Beta's frozen_date / unfreeze_date are kept as first seen by the
daily merge (refresh_beta_data only updates cancel dates), so a new member's
row never shows a later freeze. A freeze DOES add its own "Freeze Fee" row
(created = freeze start, cancelled = unfreeze), and new rows are merged in,
so an open freeze-fee row is the signal. frozen_date is still honored.

Switched OFF until Chris's journey is live: `member_check_in.enabled` must be
the JSON value true AND `go_live` a YYYY-MM-DD date; anything else means the
run only PREVIEWS who would be tagged. A Mailchimp journey ignores tags added
before it starts, so set go_live to the day it is published, and tell Isaac
the date (he stops his hand emails that day, decision 10).

Mailchimp is the ledger (fresh-eyes round 2026-10-26): before tagging, send()
reads the contact's tags. If the tag was already added on/after this
episode's start, nobody is re-tagged (the outreach-log row was lost: a failed
Drive push, a timeout after Mailchimp applied the tag) and the caller writes
the row back. Only a tag left over from an EARLIER episode is removed and
re-added, because a journey starts on tag ADD (rejoiners; the journey must
also allow re-entry).

Preview / hand check on a local Memberships.csv (no network, nothing written):
    py -m nudge_tool.member_checkin --memberships <path> --as-of 2026-10-26 --days 7
"""
from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from . import definitions, gym_metrics

TRIGGER_NAME = "member_check_in"
DEFAULTS = {"enabled": False, "tag": "send_it_member_check_in", "day": 14,
            "late_days": 7, "go_live": ""}
ALREADY_SENT = "already sent to this inbox for this membership"
# discount codes that mark a kid on an adult-priced plan (Memberships.csv
# perpetual_discount / discount, e.g. "COREKIDS - 16.00"); checked 2026-10-26
YOUTH_DISCOUNT_WORDS = ("kids", "youth")


def load_cfg(client) -> dict:
    """The client.json `member_check_in` block over DEFAULTS. ClientConfig
    keeps no raw config, so the block is read from the file itself. Never
    raises: a block that is not an object, or a bad day/late_days, falls
    back to the defaults (OFF) and says why in cfg["_problem"]."""
    cfg = dict(DEFAULTS)
    try:
        raw = json.loads((client.client_dir / "client.json").read_text(encoding="utf-8"))
        blk = raw.get(TRIGGER_NAME) if isinstance(raw, dict) else None
    except (OSError, ValueError) as exc:
        cfg["_problem"] = f"client.json unreadable ({type(exc).__name__})"
        return cfg
    if blk is None:
        return cfg
    if not isinstance(blk, dict):
        cfg["_problem"] = f"`{TRIGGER_NAME}` is not an object ({blk!r}); left OFF"
        return cfg
    cfg.update({k: v for k, v in blk.items() if not str(k).startswith("_")})

    def off(why: str) -> None:
        cfg["_problem"] = f"{why}; left OFF"
        cfg["enabled"] = False

    for k in ("day", "late_days"):
        v = cfg[k]
        if isinstance(v, bool) or not isinstance(v, (int, str)):
            ok = False
        else:
            try:
                cfg[k] = int(v)
                ok = cfg[k] >= 0
            except ValueError:
                ok = False
        if not ok:
            cfg.update({k2: DEFAULTS[k2] for k2 in ("day", "late_days")})
            off(f"`{k}` = {blk.get(k)!r} is not a whole number")
    if cfg.get("enabled") not in (True, False):
        off(f"`enabled` = {blk.get('enabled')!r} must be true or false (no quotes)")
    if str(cfg.get("go_live") or "").strip() and go_live_date(cfg) is None:
        off(f"`go_live` = {blk.get('go_live')!r} is not a YYYY-MM-DD date")
    if not str(cfg.get("tag") or "").startswith("send_it_"):
        off(f"`tag` = {blk.get('tag')!r} must start with send_it_")
    return cfg


def go_live_date(cfg: dict) -> date | None:
    """The go-live date, or None when blank or not a YYYY-MM-DD date."""
    s = str(cfg.get("go_live") or "").strip()
    if len(s) != 10:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def is_live(cfg: dict) -> bool:
    """ON only for the JSON value true (a quoted "false" is not ON) plus a
    real go_live date."""
    return cfg.get("enabled") is True and go_live_date(cfg) is not None


@dataclass
class Candidate:
    climber_id: str
    name: str
    first_name: str
    email: str
    start: date          # episode start (first plan's created_date)
    due: date            # start + day
    plan: str            # the plan held on the run date, for the report
    skip: str = ""       # "" = tag it; else the reason it is held back
    sent_on: str = ""    # set by send(): the day Mailchimp shows the tag added
                         # when it already had it ("" = today's run)


@dataclass
class Result:
    due: list = field(default_factory=list)       # Candidate, skip == ""
    held: list = field(default_factory=list)      # Candidate, skip != ""
    not_yet_live: int = 0                         # due day before go_live


def _d(v) -> date | None:
    return gym_metrics._d(v)


def _is_freeze_fee(r: dict) -> bool:
    parts = [x.strip().lower() for x in str(r.get("prices") or "").split("|") if x.strip()]
    return bool(parts) and all("freeze fee" in x for x in parts)


def _is_youth(r: dict) -> bool:
    disc = " ".join(str(r.get(k) or "") for k in ("perpetual_discount", "discount")).lower()
    return definitions.is_youth_plan(r.get("prices") or "") or any(w in disc for w in YOUTH_DISCOUNT_WORDS)


def _running(r: dict, asof: date) -> bool:
    """The row's plan is held on `asof` (started, not yet ended)."""
    s, e = _d(r.get("created_date")), _d(r.get("cancelled_date"))
    return s is not None and s <= asof and (e is None or e > asof)


def find(memberships, asof: date, cfg: dict, *, log=None, unsubscribed=(),
         staff_emails=(), skip_emails=(), min_days_between: int = 0,
         ignore_go_live: bool = False) -> Result:
    """Who gets the day-14 tag on the run dated `asof`. Pure: no network,
    no writes. `memberships` = a Memberships.csv path or its rows; `log` = an
    outreach.OutreachLog (None = no send history); `skip_emails` = inboxes
    getting another email from today's run. `ignore_go_live` is for previews
    of a switched-off tagger: it shows who is due exactly today (no late
    window, since a preview has no send history to tell who already got it)."""
    rows = [r for r in gym_metrics._rows(memberships) if not _is_youth(r)]
    by_cid: dict[str, list] = {}
    for r in rows:
        cid = str(r.get("climber_key") or "").strip()
        if cid:
            by_cid.setdefault(cid, []).append(r)
    eps, _skipped = gym_metrics._beta_episodes(rows, asof)
    day, late = int(cfg.get("day", 14)), int(cfg.get("late_days", 7))
    if ignore_go_live:
        late = 0
    gl = None if ignore_go_live else go_live_date(cfg)
    unsub = {e.strip().lower() for e in unsubscribed}
    staff = {e.strip().lower() for e in staff_emails if e}
    today = {e.strip().lower() for e in skip_emails if e}
    res = Result()
    seen: set[str] = set()
    for e in sorted(eps, key=lambda e: (e["start"], e["cid"])):
        due = e["start"] + timedelta(days=day)
        if not (due <= asof <= due + timedelta(days=late)):
            continue
        mine = by_cid.get(e["cid"], [])
        plans = [r for r in mine if not _is_freeze_fee(r) and (_d(r.get("created_date")) or asof) <= asof]
        if gl is not None and due < gl:
            res.not_yet_live += 1
            continue
        held_now = [r for r in plans if _running(r, asof)]
        show = max(held_now or plans or [{}], key=lambda r: str(r.get("created_date") or ""))
        email = str(show.get("email") or "").strip()
        c = Candidate(e["cid"], " ".join(x for x in (str(show.get("first") or "").strip(),
                                                    str(show.get("last") or "").strip()) if x),
                      str(show.get("first") or "").strip(), email, e["start"], due,
                      str(show.get("prices") or "").strip())
        key = email.lower()
        # only freezes of THIS membership (an old never-closed Freeze Fee row
        # must not hold a rejoiner forever; fresh-eyes round 2)
        frozen = any(_is_freeze_fee(r) and _running(r, asof)
                     and (_d(r.get("created_date")) or asof) >= e["start"] for r in mine) or \
            bool(e["open_rows"]) and all(r["frozen"] for r in e["open_rows"])
        if e["end"] is not None:
            c.skip = "membership ended"
        elif frozen:
            c.skip = "frozen"
        elif held_now and all(_d(r.get("cancelled_date")) for r in held_now):
            c.skip = "cancel already scheduled"
        elif not email:
            c.skip = "no email"
        elif key in staff:
            c.skip = "staff"
        elif key in unsub:
            c.skip = "unsubscribed in Mailchimp"
        elif log is not None and any(d >= e["start"].isoformat()
                                     for d in log.sent_dates(email, TRIGGER_NAME)):
            c.skip = ALREADY_SENT
        elif key in seen:
            c.skip = "same email as another member today"
        elif key in today:
            c.skip = "gets another email today (retried tomorrow)"
        elif log is not None and min_days_between > 0 and log.last_sent_any(email) and \
                (asof - date.fromisoformat(log.last_sent_any(email)[:10])).days < min_days_between:
            c.skip = f"spacing guard (another email under {min_days_between} days ago)"
        if c.skip:
            res.held.append(c)
        else:
            seen.add(key)
            res.due.append(c)
    return res


def staff_emails_of(client, ds=None) -> set[str]:
    """Staff inboxes: the config list plus anyone ingest flagged as staff
    (any email with a staff plan row, ever)."""
    out = {e.strip().lower() for e in client.reporting.get("staff_emails", []) if e}
    if ds is not None:
        out |= {(c.email or "").strip().lower() for c in ds.climbers.values()
                if getattr(c, "is_staff", False) and c.email}
    return out


def send(mc, c: Candidate, tag: str) -> str:
    """Tag one member. Returns "tagged", or "already" when Mailchimp shows the
    tag added on/after this episode's start (no re-tag, no second email).
    A tag left from an earlier episode is removed first so the journey,
    which starts on tag ADD, takes the rejoiner again."""
    mc.upsert_member(c.email, c.first_name)
    have = [t for t in mc.member_tags(c.email)
            if str(t.get("name") or "").strip().lower() == tag.lower()]
    # a tag with no readable date counts as THIS membership's (never risk a
    # second email); its log row then carries today's date
    mine = [str(t.get("date_added") or "")[:10] for t in have
            if len(str(t.get("date_added") or "")) < 10
            or str(t.get("date_added"))[:10] >= c.start.isoformat()]
    if mine:
        c.sent_on = max(d for d in mine if len(d) == 10) if any(len(d) == 10 for d in mine) else ""
        return "already"
    if have:
        mc.remove_tag(c.email, tag)
    mc.add_tag(c.email, tag)
    return "tagged"


def log_rows(sent: list[Candidate], asof: date, tag: str) -> list[dict]:
    """Outreach-log rows (mode live, Mailchimp = blank esp). A restored row
    is dated the day Mailchimp added the tag, so the stats push matches the
    email that really went out that day."""
    return [{"sent_date": c.sent_on or asof.isoformat(), "email": c.email, "climber_id": c.climber_id,
             "trigger_name": TRIGGER_NAME, "tag": tag, "mode": "live", "esp": ""} for c in sent]


def report_lines(res: Result, cfg: dict, live: bool, asof: date | None = None) -> list[str]:
    gl = go_live_date(cfg)
    if live and asof is not None and gl is not None and asof < gl:
        state = f"ON from {gl} (nothing tagged before then)"
    elif live:
        state = "LIVE"
    else:
        state = "OFF (preview only: who is due exactly today, nobody tagged)"
    head = (f"  member check-in (day {cfg.get('day', 14)}): {state}"
            f", go_live={cfg.get('go_live') or '(not set)'}")
    lines = [head]
    if cfg.get("_problem"):
        lines.append(f"    CONFIG PROBLEM: {cfg['_problem']}")
    # people already sent sit in the late window for a week; count them, don't list them
    done = [c for c in res.held if c.skip == ALREADY_SENT]
    held = [c for c in res.held if c.skip != ALREADY_SENT]
    lines.append(f"    due today: {len(res.due)}  held back: {len(held)}  already sent: {len(done)}"
                 + (f"  due before go-live (never sent): {res.not_yet_live}" if res.not_yet_live else ""))
    for c in res.due:
        lines.append(f"    {'tag ' if live else 'would'} {c.name:<22.22} {c.email}  "
                     f"(member since {c.start}, {c.plan[:40]})")
    for c in held:
        lines.append(f"    held  {c.name:<22.22} {c.email or '(no email)'}  ({c.skip})")
    return lines


def main() -> None:
    ap = argparse.ArgumentParser(description="Preview the day-14 member check-in (no network).")
    ap.add_argument("--memberships", required=True, help="path to a Beta Memberships.csv")
    ap.add_argument("--as-of", default=None, help="YYYY-MM-DD (default today)")
    ap.add_argument("--days", type=int, default=1, help="preview this many run days ending on --as-of")
    a = ap.parse_args()
    end = date.fromisoformat(a.as_of) if a.as_of else date.today()
    cfg = dict(DEFAULTS)
    with open(Path(a.memberships), encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    for i in range(a.days - 1, -1, -1):
        d = end - timedelta(days=i)
        res = find(rows, d, cfg, ignore_go_live=True)
        print(f"\n=== run {d} ===")
        for line in report_lines(res, cfg, live=False)[1:]:
            print(line)


if __name__ == "__main__":
    main()
