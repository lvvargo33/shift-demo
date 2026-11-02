"""Weekly snapshot of how many members the gym has.

WHY THIS EXISTS (2026-09-02, Chris's "member count + trend line" ask)

`member_status.csv` is a SNAPSHOT. It says who is a member right now and
nothing at all about who was a member six weeks ago. The trend line Chris
asked for therefore CANNOT be rebuilt after the fact, and two independent
facts make that permanent, not a bug to fix:

1. **Most active members carry no dates.** Measured on the real ABC file
   2026-09-02: of 288 members with status OK, **169 have no membership_start
   and no membership_exp at all**. They are recurring EFT plans and RGP never
   fills those fields. 85 have a future expiry, 34 an expired one. So the
   obvious "was this person active on date X" test is impossible for well
   over half the roster. Reconstructing it that way returns 85 members for a
   day the gym really had 294, which is how this was caught.
2. **Nothing records a status CHANGE.** A member who froze, cancelled, or
   came back leaves no dated trace anywhere in what we pull, only a new
   current value that overwrites the old one.

So the only honest way to get a member trend is to start writing one down.
This module does exactly that: one row per ISO week, appended by the daily
send cron. The line is short at first and correct forever after.

Deliberately the same shape as `member_followups.py` (append-only CSV, latest
row per key wins, optional Drive id, missing file means "nothing yet, not an
error"), because that pattern is already deployed and trusted.

WHY WEEKLY, NOT DAILY
Chris asked for weekly points over 12 weeks. A daily row would be 7x the file
for no extra signal, and membership dues bill monthly anyway, so day-to-day
movement is mostly noise. `record()` is idempotent within an ISO week: the
cron may call it every single day and only the first call of each week
writes. That also means a missed cron day costs nothing.
"""
from __future__ import annotations

import csv
import os
from datetime import date, datetime, timedelta

from .config import ClientConfig

FIELDS = ["week_ending", "recorded_at", "active", "frozen", "terminated",
          "sendable"]

# Drive file id env var. Same pattern as OUTREACH_DRIVE_ID and
# MEMBER_FOLLOWUPS_DRIVE_ID: pre-create an empty CSV in the client's Drive
# folder, share it with the service account as Editor, paste the id here.
# Per-service on Render. Unset = local file only, never an error.
DRIVE_ENV = "MEMBER_COUNTS_DRIVE_ID"

# How many weekly points the Overview trend line shows.
TREND_WEEKS = 12


def drive_id() -> str:
    return (os.getenv(DRIVE_ENV) or "").strip()


def _clean(v) -> str:
    return str(v if v is not None else "").strip()


def _int(v) -> int:
    try:
        return int(str(v).strip() or 0)
    except (TypeError, ValueError):
        return 0


def week_ending(d: date) -> str:
    """The Sunday that closes `d`'s ISO week, as an ISO date string.

    ISO weeks run Monday (weekday 0) to Sunday (weekday 6), so the closing
    Sunday is always `d + (6 - d.weekday())` days. Every day inside one week
    maps to the same string, which is what makes `record()` idempotent."""
    return (d + timedelta(days=6 - d.weekday())).isoformat()


# --------------------------------------------------------------------------
# file I/O
# --------------------------------------------------------------------------

def load_rows(client: ClientConfig) -> list[dict]:
    """Every row in file order. Missing file -> [] (no snapshots taken yet)."""
    path = client.member_counts_csv
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return [{k: _clean(row.get(k, "")) for k in FIELDS}
                for row in csv.DictReader(f)]


def _append(client: ClientConfig, row: dict) -> None:
    path = client.member_counts_csv
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in FIELDS})


def recorded_weeks(rows: list[dict]) -> set:
    return {r["week_ending"] for r in rows if r["week_ending"]}


def record(client: ClientConfig, asof: date, counts) -> bool:
    """Write this week's snapshot unless it is already on file.

    `counts` is an `audience.Counts` (or anything exposing `.active`,
    `.sendable`, `.inactive_by_status`). Returns True if a row was written.

    Idempotent per ISO week on purpose: the 11 AM cron calls this every day
    and only the first run of each week appends. Returns False, quietly, when
    the week is already recorded or when `counts` is None (a missing or
    unreadable member file must never break a send run)."""
    if counts is None:
        return False
    wk = week_ending(asof)
    if wk in recorded_weeks(load_rows(client)):
        return False
    by_status = {str(k).upper(): v
                 for k, v in (getattr(counts, "inactive_by_status", {}) or {}).items()}
    _append(client, {
        "week_ending": wk,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "active": getattr(counts, "active", 0),
        "frozen": by_status.get("FROZEN", 0),
        "terminated": by_status.get("TERMINATED", 0),
        "sendable": getattr(counts, "sendable", 0),
    })
    return True


def series(client: ClientConfig, weeks: int = TREND_WEEKS,
           rows: list[dict] | None = None) -> list[dict]:
    """The last `weeks` weekly points, oldest first.

    Latest row per week wins, so a hand-corrected re-append supersedes the
    cron's row without anyone editing history. Only weeks actually recorded
    appear: gaps are NOT filled with zeros, because a zero would read as "the
    gym lost every member that week" instead of "nobody wrote it down"."""
    by_week: dict = {}
    for r in (load_rows(client) if rows is None else rows):
        if r.get("week_ending"):
            by_week[r["week_ending"]] = r
    out = [{"week_ending": w,
            "active": _int(by_week[w]["active"]),
            "frozen": _int(by_week[w]["frozen"]),
            "sendable": _int(by_week[w]["sendable"])}
           for w in sorted(by_week)]
    return out[-weeks:] if weeks and weeks > 0 else out


def label(week_ending_iso: str) -> str:
    """'2026-09-06' -> 'Sep 6', the x-axis tick."""
    try:
        d = date.fromisoformat(week_ending_iso)
    except (TypeError, ValueError):
        return week_ending_iso or ""
    return f"{d.strftime('%b')} {d.day}"


def build_block(client: ClientConfig, counts, weeks: int = TREND_WEEKS,
                rows: list[dict] | None = None) -> dict:
    """The `payload["members"]["member_count"]` block.

    `current` comes from TODAY's live audience file, not from the snapshot
    log, so the headline number is never stale even if the cron missed a week.
    `points` is the recorded history. `change` compares current against the
    oldest point on file and is None until there are at least two points, so
    the UI never shows a made-up delta."""
    pts = series(client, weeks=weeks, rows=rows)
    current = getattr(counts, "active", None) if counts is not None else None
    frozen = None
    if counts is not None:
        by_status = {str(k).upper(): v
                     for k, v in (getattr(counts, "inactive_by_status", {}) or {}).items()}
        frozen = by_status.get("FROZEN", 0)
    change = None
    if current is not None and len(pts) >= 2:
        change = current - pts[0]["active"]
    return {
        "current": current,
        "frozen": frozen,
        "points": pts,
        "labels": [label(p["week_ending"]) for p in pts],
        "values": [p["active"] for p in pts],
        "weeks_tracked": len(pts),
        "weeks_target": weeks,
        "change_since_first": change,
        "first_week": pts[0]["week_ending"] if pts else "",
        # The UI shows this instead of a chart until the line means something.
        "building": len(pts) < 2,
    }


# --------------------------------------------------------------------------
# Drive sync (optional at every touch point, exactly like member_followups)
# --------------------------------------------------------------------------

def pull(client: ClientConfig, svc=None) -> bool:
    """Pull the canonical CSV from Drive if the id is set. Returns True if
    pulled. Never raises on an unset id; a Drive error propagates so the
    caller can decide."""
    fid = drive_id()
    if not fid:
        return False
    from . import drive_io
    drive_io.pull(fid, str(client.member_counts_csv),
                  svc=svc or drive_io._service(drive_io._RO))
    return True


def push(client: ClientConfig, svc=None) -> bool:
    """Push the local CSV to its Drive id if set AND the file exists."""
    fid = drive_id()
    if not fid or not client.member_counts_csv.exists():
        return False
    from . import drive_io
    drive_io.push(str(client.member_counts_csv), file_id=fid,
                  svc=svc or drive_io._service(drive_io._RW))
    return True
