"""Follow-up state for membership-survey responses.

Membership Surveys SPEC, "Session 2026-08-21 (late pm)", decision: follow-up
state (status, assignee, notes) persists in a per-client APPEND-ONLY CSV,
`clients/<slug>/member_followups.csv`, written by the dashboard's
`POST /followup` and synced to Drive exactly like the outreach log. There is no
database; this file IS the store.

WHY APPEND-ONLY
One row per change, never an edit in place. The latest row per `mid` wins for
status and assignee; notes accumulate. That gives an audit trail for free
("who moved this to resolved, and when"), survives two staff saving at once
(two appends cannot clobber each other the way two rewrites can), and is the
same shape as the outreach log the team already trusts.

WHY `mid`
`mid` is the customerGuid, the only join key the survey carries (the sheet's
email/name columns are always blank on a web-link collector). A follow-up row
therefore belongs to a RESPONSE, which the parser dedupes to the latest per
mid, so a member re-surveyed 180 days later starts with a clean card only if
staff resolved the old one; an open card carries over, which is what you want
("they were unhappy last time, and here they are again").

OPTIONAL AT EVERY TOUCH POINT
Like `member_status.csv`, the Drive id (`MEMBER_FOLLOWUPS_DRIVE_ID`) may be
unset. Unset = local file only, never an error. A missing local file = no
follow-ups yet. Nothing in the FTV path depends on this module.
"""
from __future__ import annotations

import csv
import os
from datetime import datetime

from .config import ClientConfig

FIELDS = ["ts", "mid", "status", "assignee", "note", "by"]

# Lovable `WorkStatus`, all four. The demo had three; "waiting" is new and
# open question 21 asks whether ABC will use it.
STATUSES = ("open", "in_progress", "waiting", "resolved")
STATUS_LABEL = {
    "open": "Open",
    "in_progress": "In progress",
    "waiting": "Waiting on member",
    "resolved": "Resolved",
}
DEFAULT_STATUS = "open"

# Drive file id env var. Same pattern as OUTREACH_DRIVE_ID: pre-create an empty
# file in the client's Drive folder, share it with the service account as
# Editor, paste the id. Per-service on Render.
DRIVE_ENV = "MEMBER_FOLLOWUPS_DRIVE_ID"


class FollowupError(ValueError):
    """A bad write request (unknown status, blank mid). Never raised on read."""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clean(v) -> str:
    return str(v if v is not None else "").strip()


# --------------------------------------------------------------------------
# file I/O
# --------------------------------------------------------------------------

def load_rows(client: ClientConfig) -> list[dict]:
    """Every row in file order. Missing file -> [] (no follow-ups yet)."""
    path = client.member_followups_csv
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return [{k: _clean(row.get(k, "")) for k in FIELDS}
                for row in csv.DictReader(f)]


def append(client: ClientConfig, rows: list[dict]) -> int:
    """Append rows. Creates the header on first write. Returns rows written."""
    if not rows:
        return 0
    path = client.member_followups_csv
    # an empty file (a freshly pulled empty Drive copy) needs the header too
    new_file = not path.exists() or path.stat().st_size == 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        for r in rows:
            w.writerow({k: _clean(r.get(k, "")) for k in FIELDS})
    return len(rows)


# --------------------------------------------------------------------------
# merge: rows -> current state per mid
# --------------------------------------------------------------------------

def merge(rows) -> dict:
    """rows (any order) -> {mid: state}.

    state = {status, assignee, notes: [{at, text, by}], updated_at, by}.
    Rows are applied in (ts, file order) so the newest non-blank status and
    assignee win; a blank field on a row means "unchanged", which is how the
    POST endpoint expresses a note-only or status-only change. An assignee of
    "-" clears the assignee (the UI's "Unassigned" choice), because a blank
    cannot mean both "unchanged" and "clear"."""
    ordered = sorted(enumerate(rows), key=lambda ir: (ir[1].get("ts", ""), ir[0]))
    out: dict = {}
    for _, r in ordered:
        mid = _clean(r.get("mid"))
        if not mid:
            continue
        st = out.setdefault(mid, {"status": DEFAULT_STATUS, "assignee": "",
                                  "notes": [], "updated_at": "", "by": ""})
        status = _clean(r.get("status"))
        if status in STATUSES:
            st["status"] = status
        assignee = _clean(r.get("assignee"))
        if assignee == "-":
            st["assignee"] = ""
        elif assignee:
            st["assignee"] = assignee
        note = _clean(r.get("note"))
        if note:
            st["notes"].append({"at": _clean(r.get("ts")), "text": note,
                                "by": _clean(r.get("by"))})
        st["updated_at"] = _clean(r.get("ts")) or st["updated_at"]
        st["by"] = _clean(r.get("by")) or st["by"]
    return out


def latest_by_mid(client: ClientConfig) -> dict:
    return merge(load_rows(client))


# --------------------------------------------------------------------------
# the write path used by POST /followup
# --------------------------------------------------------------------------

def record(client: ClientConfig, mid: str, status: str | None = None,
           assignee: str | None = None, note: str | None = None,
           by: str | None = None, ts: str | None = None,
           allowed_assignees=None) -> dict:
    """Append ONE change row and return the merged state for `mid`.

    `status` must be one of STATUSES when given. `assignee` must be in
    `allowed_assignees` when that list is given (the fixed list in client.json,
    decision 2026-08-21), or "" / "-" to clear. A request that changes nothing
    (all three None/blank) is rejected: it would write a row that means
    nothing and still bump `updated_at`."""
    mid = _clean(mid)
    if not mid:
        raise FollowupError("mid is required")
    status = _clean(status)
    if status and status not in STATUSES:
        raise FollowupError(f"unknown status {status!r}; "
                            f"expected one of {', '.join(STATUSES)}")
    assignee_raw = assignee
    assignee = _clean(assignee)
    if assignee_raw is not None and not assignee:
        assignee = "-"          # explicit clear
    if assignee and assignee != "-" and allowed_assignees is not None \
            and assignee not in allowed_assignees:
        raise FollowupError(f"unknown assignee {assignee!r}; expected one of "
                            f"{', '.join(allowed_assignees) or '(none configured)'}")
    note = _clean(note)
    if not (status or assignee or note):
        raise FollowupError("nothing to record (no status, assignee or note)")
    row = {"ts": ts or _now(), "mid": mid, "status": status,
           "assignee": assignee, "note": note, "by": _clean(by)}
    append(client, [row])
    return merge(load_rows(client)).get(mid, {})


# --------------------------------------------------------------------------
# Drive sync (optional)
# --------------------------------------------------------------------------

def drive_id() -> str:
    return (os.getenv(DRIVE_ENV) or "").strip()


def pull(client: ClientConfig, svc=None) -> bool:
    """Pull the canonical CSV from Drive if the id is set. Returns True if
    pulled. Never raises on an unset id; a Drive error propagates so the
    caller can decide (the dashboard degrades, the cron prints a note)."""
    fid = drive_id()
    if not fid:
        return False
    from . import drive_io
    drive_io.pull(fid, str(client.member_followups_csv),
                  svc=svc or drive_io._service(drive_io._RO))
    return True


def push(client: ClientConfig, svc=None) -> bool:
    """Push the local CSV to its Drive id if set AND the file exists."""
    fid = drive_id()
    if not fid or not client.member_followups_csv.exists():
        return False
    from . import drive_io
    drive_io.push(str(client.member_followups_csv), file_id=fid,
                  svc=svc or drive_io._service(drive_io._RW))
    return True
