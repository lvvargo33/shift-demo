"""Saved reply edits: the template text a staffer started from and the text
they actually sent, kept as a pair.

ROADMAP_SPEC_2026-09.md, block 3 (Luke 2026-09-04, green-lit). Staff can now
change the subject and body of a member reply on the response page before
Send. Every send from that page writes ONE row here, edited or not, so the
file is the full record of "what did the template say vs what went out". An
unedited send is a row with `changed=0`; that is deliberate, it is the
baseline the LLM-drafting idea (block 8) needs.

SAME SHAPE AS member_followups.py
Append-only CSV in the client folder, `clients/<slug>/member_reply_edits.csv`,
optionally mirrored to a Drive file (`MEMBER_REPLY_EDITS_DRIVE_ID`, web service
only: the send cron never writes replies). Pre-create the Drive file WITH its
header row: `append` only writes a header when the local file does not exist,
and a pulled empty file exists.

FAIL-SOFT, ALWAYS
The email has already gone out by the time this is written. A failure here is
printed and reported to the page as `edit_saved=false`; it never turns a sent
reply into an error.
"""
from __future__ import annotations

import csv
import os
from datetime import datetime

from .config import ClientConfig

FIELDS = ["ts", "mid", "template_key", "by", "original_subject", "edited_subject",
          "original_body", "edited_body", "changed"]

DRIVE_ENV = "MEMBER_REPLY_EDITS_DRIVE_ID"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clean(v) -> str:
    return str(v if v is not None else "").strip()


def _norm(text: str) -> str:
    """Whitespace-insensitive form for the changed test: a browser textarea
    turns \\n into \\r\\n, and a trailing newline is not an edit."""
    return "\n".join(line.rstrip() for line in _clean(text).replace("\r\n", "\n").split("\n")).strip()


def is_changed(original_subject: str, edited_subject: str,
               original_body: str, edited_body: str) -> bool:
    return (_norm(original_subject) != _norm(edited_subject)
            or _norm(original_body) != _norm(edited_body))


# --------------------------------------------------------------------------
# file I/O
# --------------------------------------------------------------------------

def load_rows(client: ClientConfig) -> list[dict]:
    """Every row in file order. Missing file -> []."""
    path = client.member_reply_edits_csv
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return [{k: _clean(row.get(k, "")) for k in FIELDS}
                for row in csv.DictReader(f)]


def append(client: ClientConfig, rows: list[dict]) -> int:
    """Append rows. Creates the header on first write. Returns rows written."""
    if not rows:
        return 0
    path = client.member_reply_edits_csv
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


def make_row(mid: str, template_key: str, by: str, original_subject: str,
             edited_subject: str, original_body: str, edited_body: str,
             ts: str | None = None) -> dict:
    """One pair row. `changed` is computed here so every writer agrees."""
    return {
        "ts": ts or _now(),
        "mid": _clean(mid),
        "template_key": _clean(template_key),
        "by": _clean(by),
        "original_subject": _clean(original_subject),
        "edited_subject": _clean(edited_subject),
        "original_body": _clean(original_body),
        "edited_body": _clean(edited_body),
        "changed": "1" if is_changed(original_subject, edited_subject,
                                     original_body, edited_body) else "0",
    }


def record(client: ClientConfig, **kw) -> dict:
    """Append one pair (see make_row for the keyword arguments) and return it."""
    row = make_row(**kw)
    append(client, [row])
    return row


def summary(rows) -> dict:
    """{rows, changed} for the Settings card."""
    rows = list(rows or [])
    return {"rows": len(rows),
            "changed": sum(1 for r in rows if _clean(r.get("changed")) == "1")}


# --------------------------------------------------------------------------
# Drive sync (optional)
# --------------------------------------------------------------------------

def drive_id() -> str:
    return (os.getenv(DRIVE_ENV) or "").strip()


def pull(client: ClientConfig, svc=None) -> bool:
    """Pull the canonical CSV from Drive if the id is set. Returns True if
    pulled. Never raises on an unset id; a Drive error propagates so the
    caller can decide (every caller here catches and prints)."""
    fid = drive_id()
    if not fid:
        return False
    from . import drive_io
    drive_io.pull(fid, str(client.member_reply_edits_csv),
                  svc=svc or drive_io._service(drive_io._RO))
    return True


def push(client: ClientConfig, svc=None) -> bool:
    """Push the local CSV to its Drive id if set AND the file exists."""
    fid = drive_id()
    if not fid or not client.member_reply_edits_csv.exists():
        return False
    from . import drive_io
    drive_io.push(str(client.member_reply_edits_csv), file_id=fid,
                  svc=svc or drive_io._service(drive_io._RW))
    return True
