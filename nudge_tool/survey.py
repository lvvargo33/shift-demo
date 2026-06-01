"""Survey loop reader + matcher (FTV_PILOT_SPEC.md section 14, S2).

The survey says WHY. A Google Form (4 Qs, section 14.4) writes to a Sheet; this
module reads that sheet (live via the gsheets bridge, or a local CSV fixture for
dev), normalizes each row, and matches it to a Beta climber BY EMAIL
(lowercased, trimmed, section 14.3). A matched response fills the climber's
survey_* fields so the survey-anchored triggers (nudge_pricing, ...) and the
MANUAL safety precedence start firing. Unmatched responses are kept in a bucket
(shown, not dropped, not acted on).

This module reads + matches only. It never sends and never writes to the sheet.
The actual survey email is a Mailchimp Journey on the survey_request tag (S5).

Source is config-driven (client.json "survey"):
  source: "csv"    -> read csv_path (a local file; dev/testing)
  source: "gsheet" -> read gsheet_id/gsheet_range via the gsheets bridge
"""
from __future__ import annotations

import csv
import io
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .config import ClientConfig
from .ingest import Dataset

GSHEETS = Path("C:/Users/18642/.claude/tools/gsheets/gsheets.py")


@dataclass
class SurveyResponse:
    email: str
    answered_at: str          # YYYY-MM-DD (survey_sent anchor approximation)
    q1_overall: str
    q2_intent: str
    q3_blocker_raw: str
    q4_text: str
    blocker: str | None       # mapped Q3 -> internal value (None/"" = no blocker)
    intent: str | None        # mapped Q2 -> bucket
    safety: bool              # Q4 tripped a safety keyword
    matched: bool = False     # email found a Beta climber
    climber_id: str = ""
    name: str = ""


@dataclass
class SurveyResult:
    responses: list[SurveyResponse] = field(default_factory=list)
    matched: int = 0
    unmatched: int = 0
    by_blocker: dict = field(default_factory=dict)
    safety_count: int = 0

    @property
    def unmatched_responses(self) -> list[SurveyResponse]:
        return [r for r in self.responses if not r.matched]


def _norm_email(s: str) -> str:
    return (s or "").strip().lower()


# Google Sheets / Excel serial-date epoch (day 0 = 1899-12-30).
_SERIAL_EPOCH = datetime(1899, 12, 30)


def _d(s: str) -> str:
    """Normalize a survey timestamp to YYYY-MM-DD.

    Handles the three real-world shapes a Form timestamp can arrive in:
      - Google Sheets serial number (e.g. '46174.4848') -- what the gsheets
        bridge returns, since it reads UNFORMATTED values.
      - US 'm/d/yyyy [h:mm:ss]' -- a formatted Form/Sheets export.
      - ISO 'YYYY-MM-DD...' -- the S2 dev CSV fixture (prior behavior).
    Returns '' when it can't parse one (matched climbers with no parsable
    date simply skip survey_sent anchoring rather than carrying garbage)."""
    s = (s or "").strip()
    if not s:
        return ""
    # Google Sheets serial date: a bare (possibly fractional) number.
    try:
        serial = float(s)
    except ValueError:
        serial = None
    if serial is not None:
        # Guard to a plausible date range (~1982..~2119) so a stray "5"
        # (e.g. an overall-rating value) never masquerades as a date.
        if 30000 <= serial <= 80000:
            return (_SERIAL_EPOCH + timedelta(days=serial)).strftime("%Y-%m-%d")
        return ""
    # US m/d/yyyy, optionally with a time after a space.
    if "/" in s:
        datepart = s.split(" ", 1)[0]
        for fmt in ("%m/%d/%Y", "%m/%d/%y"):
            try:
                return datetime.strptime(datepart, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    # ISO-ish: keep the date prefix (unchanged from the original behavior).
    return s[:10]


def _read_rows(client: ClientConfig) -> list[dict]:
    """Return the response sheet as a list of dict rows (header -> value)."""
    sv = client.survey
    source = sv.get("source", "csv")
    if source == "csv":
        path = sv.get("csv_path", "")
        p = Path(path)
        if not p.is_absolute():
            # survey file is a per-client file -> resolve from the client folder
            p = client.client_dir / path
        if not p.exists():
            return []
        with open(p, encoding="utf-8") as f:
            return list(csv.DictReader(f))
    if source == "gsheet":
        sheet_id = sv.get("gsheet_id", "").strip()
        rng = sv.get("gsheet_range", "")
        if not sheet_id:
            return []
        # Cloud path: a service-account key (no interactive Google login). Set
        # GSHEETS_SA_KEY to a key-file path, or GSHEETS_SA_JSON to the raw JSON.
        # When neither is set we fall back to the local gsheets bridge (Luke's
        # ADC login), so existing on-machine runs behave exactly as before.
        if os.getenv("GSHEETS_SA_KEY") or os.getenv("GSHEETS_SA_JSON"):
            return _read_gsheet_sa(sheet_id, rng)
        out = subprocess.run(
            [sys.executable, str(GSHEETS), "read", sheet_id, rng],
            capture_output=True, text=True, timeout=180,
        )
        if out.returncode != 0:
            raise RuntimeError(f"gsheets read failed: {out.stderr.strip()}")
        return list(csv.DictReader(io.StringIO(out.stdout)))
    raise RuntimeError(f"unknown survey source '{source}' (use csv or gsheet)")


_SHEETS_RO = "https://www.googleapis.com/auth/spreadsheets.readonly"


def _read_gsheet_sa(sheet_id: str, rng: str) -> list[dict]:
    """Read the response sheet with a service-account key (cloud/headless).
    Returns the same list-of-dict shape as the CSV/bridge readers."""
    import json
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    key_path = os.getenv("GSHEETS_SA_KEY")
    if key_path:
        creds = service_account.Credentials.from_service_account_file(
            key_path, scopes=[_SHEETS_RO])
    else:
        info = json.loads(os.getenv("GSHEETS_SA_JSON", "{}"))
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=[_SHEETS_RO])
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    values = (svc.spreadsheets().values()
              .get(spreadsheetId=sheet_id, range=rng).execute()
              .get("values", []))
    if not values:
        return []
    header = values[0]
    rows = []
    for raw in values[1:]:
        raw = list(raw) + [""] * (len(header) - len(raw))  # pad trailing blanks
        rows.append({h: raw[i] for i, h in enumerate(header)})
    return rows


def _map_blocker(raw: str, blocker_map: dict) -> str | None:
    s = (raw or "").strip().lower()
    if not s:
        return None
    # Longest needle first so a specific option wins over a shorter substring
    # of it (same class of fix as _map_intent; harmless when needles are disjoint).
    for needle in sorted(blocker_map, key=len, reverse=True):
        if needle and needle in s:
            return blocker_map[needle] or None
    return None


def _map_intent(raw: str, intent_map: dict) -> str | None:
    s = (raw or "").strip().lower()
    # Longest needle first: "unlikely" must be tested before "likely", else
    # "unlikely" matches the substring "likely" and mis-maps low intent to high.
    for needle in sorted(intent_map, key=len, reverse=True):
        if needle and needle in s:
            return intent_map[needle]
    return None


def _is_safety(text: str, keywords: list[str]) -> bool:
    s = (text or "").lower()
    return any(k in s for k in keywords)


def load_responses(client: ClientConfig) -> list[SurveyResponse]:
    sv = client.survey
    if not sv.get("enabled"):
        return []
    cols = sv.get("columns", {})
    blocker_map = {k.lower(): v for k, v in sv.get("blocker_map", {}).items()}
    intent_map = {k.lower(): v for k, v in sv.get("intent_map", {}).items()}
    safety_keywords = [k.lower() for k in sv.get("safety_keywords", [])]

    def col(row: dict, key: str) -> str:
        header = cols.get(key, "")
        return (row.get(header, "") or "").strip()

    out: list[SurveyResponse] = []
    for row in _read_rows(client):
        email = _norm_email(col(row, "email"))
        if not email:
            continue
        q3 = col(row, "q3_blocker")
        q4 = col(row, "q4_text")
        out.append(SurveyResponse(
            email=email,
            answered_at=_d(col(row, "timestamp")),
            q1_overall=col(row, "q1_overall"),
            q2_intent=col(row, "q2_intent"),
            q3_blocker_raw=q3,
            q4_text=q4,
            blocker=_map_blocker(q3, blocker_map),
            intent=_map_intent(col(row, "q2_intent"), intent_map),
            safety=_is_safety(q4, safety_keywords),
        ))
    return out


def apply(ds: Dataset, client: ClientConfig) -> SurveyResult:
    """Read responses, match to climbers by email, fill their survey_* fields.
    Returns a SurveyResult with match counts + the unmatched bucket. If the
    survey is disabled this is a no-op returning an empty result."""
    result = SurveyResult()
    responses = load_responses(client)
    if not responses:
        return result

    # email -> climber (last writer wins; emails are unique enough for the pilot)
    index = {}
    for c in ds.climbers.values():
        e = _norm_email(c.email)
        if e:
            index[e] = c

    for r in responses:
        c = index.get(r.email)
        if c is None:
            result.unmatched += 1
            result.responses.append(r)
            continue
        r.matched = True
        r.climber_id = c.climber_id
        r.name = c.name
        c.survey_answered = True
        if r.answered_at:
            c.survey_sent_date = r.answered_at
        c.survey_blocker = r.blocker
        c.survey_intent = r.intent
        if r.safety:
            c.safety_flag = True
            result.safety_count += 1
        if r.blocker:
            result.by_blocker[r.blocker] = result.by_blocker.get(r.blocker, 0) + 1
        result.matched += 1
        result.responses.append(r)

    return result
