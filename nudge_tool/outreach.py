"""Local outreach log: the tool's own record of every tag it has applied.

This backs precedence rules 4 + 5 (FTV_PILOT_SPEC.md section 14.7):
  - once_only triggers never repeat (e.g. survey sent once, ever)
  - cooldown triggers don't repeat within cooldown_days
and it tightens the survey_sent anchor: S2 approximates "survey sent" with the
response timestamp, but the log knows the real date we tagged survey_request, so
when a log entry exists we measure the survey-anchored windows from THAT date.

The log is a per-client append-only CSV (clients/<slug>/outreach_log.csv,
gitignored). One row per tag actually applied to a real contact. In dry-run the
tool tags nobody, so it writes nothing here; the log only grows on --test/--live
sends. Reading it is always safe and is how a later dry-run previews "already
sent, won't repeat."

This module is pure file I/O + lookups: no network, no eligibility logic.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date

from . import ingest
from .config import ClientConfig

FIELDS = ["sent_date", "email", "climber_id", "trigger_name", "tag", "mode"]


def _norm_email(s: str) -> str:
    return (s or "").strip().lower()


@dataclass
class OutreachLog:
    """Indexed view of the outreach CSV. Keys are (email_lower, tag)."""
    by_email_tag: dict = field(default_factory=dict)   # (email, tag) -> [sent_date,...]

    def sent_dates(self, email: str, tag: str) -> list[str]:
        return self.by_email_tag.get((_norm_email(email), tag), [])

    def last_sent(self, email: str, tag: str) -> str | None:
        dates = self.sent_dates(email, tag)
        return max(dates) if dates else None

    @property
    def total_rows(self) -> int:
        return sum(len(v) for v in self.by_email_tag.values())


def load(client: ClientConfig) -> OutreachLog:
    """Read the per-client outreach log. Missing file -> empty log (the normal
    state until the first --test/--live send)."""
    log = OutreachLog()
    path = client.outreach_log
    if not path.exists():
        return log
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            email = _norm_email(row.get("email", ""))
            tag = (row.get("tag", "") or "").strip()
            sent = (row.get("sent_date", "") or "").strip()[:10]
            if not (email and tag and sent):
                continue
            log.by_email_tag.setdefault((email, tag), []).append(sent)
    return log


def append(client: ClientConfig, rows: list[dict]) -> int:
    """Append applied-tag rows. Called only when a real tag is applied (T4/S5);
    a no-op for the empty list, so dry-run never writes. Creates the header on
    first write. Returns the number of rows written."""
    if not rows:
        return 0
    path = client.outreach_log
    new_file = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    return len(rows)


def load_rows(client: ClientConfig) -> list[dict]:
    """Raw outreach rows (one dict per FIELDS), in file order. Used by `cleanup`,
    which needs mode + climber_id that the indexed OutreachLog drops. Missing
    file -> []."""
    path = client.outreach_log
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return [{k: (row.get(k, "") or "") for k in FIELDS}
                for row in csv.DictReader(f)]


def rewrite(client: ClientConfig, rows: list[dict]) -> None:
    """Overwrite the outreach log with exactly `rows` (used by `cleanup` to drop
    test rows). Always writes the header; an empty list leaves a header-only
    file so the log's shape is preserved."""
    path = client.outreach_log
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def tighten_survey_sent(ds: ingest.Dataset, log: OutreachLog) -> int:
    """Override each climber's survey_sent_date with the real survey_request send
    date from the log, when we have one. S2 sets survey_sent_date to the response
    timestamp (an approximation); the actual send date is more correct for the
    survey-anchored windows. Returns how many climbers were tightened."""
    n = 0
    for c in ds.climbers.values():
        sent = log.last_sent(c.email, "survey_request")
        if sent:
            c.survey_sent_date = sent
            n += 1
    return n
