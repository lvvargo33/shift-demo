"""Beta climber-profile residency lookup (trial-swap, 2026-08-07).

SHIFT's 2-week trial is West-Michigan-residents-only (Isaac 7/31). The three
nightly Beta exports carry no address column, but the per-climber profile API
does: an authed GET /v3/<gym>/climber/list?email=... returns climber records
with an `address` field (verified live 2026-08-07 with a token minted from this
repo's .env). This module fetches that address for the FEW people a
residency-gated trigger is about to consider, extracts a zip, and caches the
answer so each climber is fetched once.

Cache: `_residency_cache.csv` next to the repo root (email, zip, checked_at),
mirrored to Drive when RESIDENCY_CACHE_DRIVE_ID is set (same fail-soft pattern
and same GOTCHA as the engagement cache: the Drive file must be an UPLOADED
CSV, never a native Google Sheet; drive_io.pull 403s on native files). A found
zip is permanent; a blank answer is retried after RETRY_EMPTY_DAYS so a climber
who fills their profile later still gets picked up.

Auth is self-contained (no import from beta_pull: this package must work from
the cloud bundle): long-lived Firebase refresh token -> 1-hour ID token.

Everything fails SOFT to "no zip": the engine's resident_zip_prefix requires
key fails CLOSED on a blank zip, so any failure here means "don't offer the
trial", never a wrong send.
"""
from __future__ import annotations

import csv
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

TOKEN_URL = "https://securetoken.googleapis.com/v1/token"
API = "https://api.sendmoregetbeta.com"
ORIGIN = "https://gym.sendmoregetbeta.com"
DEFAULT_GYM = "13353"

BASE = Path(__file__).resolve().parent.parent
CACHE_PATH = BASE / "_residency_cache.csv"
CACHE_FIELDS = ["email", "zip", "checked_at"]
RETRY_EMPTY_DAYS = 7

_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


def _http(url: str, data: bytes | None = None, method: str = "GET",
          headers: dict | None = None, timeout: int = 30) -> str:
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def mint_token() -> str:
    """Refresh token -> fresh 1-hour Firebase ID token (same recipe as
    beta_pull.mint_token; whitespace collapsed because cloud env fields wrap
    long values)."""
    api_key = re.sub(r"\s+", "", os.getenv("BETA_API_KEY", ""))
    refresh = re.sub(r"\s+", "", os.getenv("BETA_REFRESH_TOKEN", ""))
    missing = [n for n, v in (("BETA_API_KEY", api_key),
                              ("BETA_REFRESH_TOKEN", refresh)) if not v]
    if missing:
        raise RuntimeError(f"residency: missing secret(s): {', '.join(missing)}")
    body = urllib.parse.urlencode(
        {"grant_type": "refresh_token", "refresh_token": refresh}).encode()
    out = _http(f"{TOKEN_URL}?key={api_key}", data=body, method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"})
    tok = json.loads(out).get("id_token")
    if not tok:
        raise RuntimeError("residency: token refresh returned no id_token")
    return tok


def _zip_from_value(v) -> str:
    """Pull a 5-digit zip out of whatever shape the address field takes:
    a plain string, or a dict with zip-ish keys."""
    if isinstance(v, dict):
        for k in ("zip", "zipcode", "zip_code", "postcode", "postal_code",
                  "postalCode"):
            got = _zip_from_value(v.get(k))
            if got:
                return got
        # fall through: scan every string value in the dict
        v = " ".join(str(x) for x in v.values() if x)
    if not v:
        return ""
    m = None
    for m in _ZIP_RE.finditer(str(v)):
        pass  # keep the LAST 5-digit group: zips end a US address line
    return m.group(1) if m else ""


def lookup_zip(token: str, email: str, gym: str = "") -> str:
    """One climber's zip from the Beta profile API. '' when the profile has no
    address (or the record doesn't match the email exactly)."""
    gym = gym or os.getenv("BETA_GYM_ID", DEFAULT_GYM)
    q = urllib.parse.urlencode({"email": email})
    try:
        out = _http(f"{API}/v3/{gym}/climber/list?{q}",
                    headers={"authorization": token, "accept": "*/*",
                             "origin": ORIGIN, "referer": f"{ORIGIN}/"})
        data = json.loads(out)
    except (urllib.error.URLError, ValueError, OSError):
        return ""
    # Response may be a bare list or wrapped ({"data": [...]}, {"climbers": ...}).
    records = data
    if isinstance(data, dict):
        for k in ("data", "climbers", "results", "list", "items"):
            if isinstance(data.get(k), list):
                records = data[k]
                break
        else:
            records = [data]
    want = email.strip().lower()
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        rec_email = str(rec.get("email") or "").strip().lower()
        if rec_email and rec_email != want:
            continue
        z = _zip_from_value(rec.get("address"))
        if z:
            return z
    return ""


# --- cache -------------------------------------------------------------------

def _drive_id() -> str:
    return os.getenv("RESIDENCY_CACHE_DRIVE_ID", "").strip()


def load_cache() -> dict[str, tuple[str, str]]:
    """email -> (zip, checked_at). Fails soft to {} (a lost cache just costs
    refetches, never a wrong answer)."""
    if _drive_id():
        try:
            from . import drive_io
            drive_io.pull(_drive_id(), str(CACHE_PATH))
        except Exception as exc:
            print(f"  residency: cache pull failed ({exc}); using local/empty")
    if not CACHE_PATH.exists():
        return {}
    out: dict[str, tuple[str, str]] = {}
    try:
        with open(CACHE_PATH, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                em = (r.get("email") or "").strip().lower()
                if em:
                    out[em] = ((r.get("zip") or "").strip(),
                               (r.get("checked_at") or "").strip())
    except (OSError, csv.Error) as exc:
        print(f"  residency: cache unreadable ({exc}); starting empty")
        return {}
    return out


def save_cache(cache: dict[str, tuple[str, str]]) -> None:
    try:
        with open(CACHE_PATH, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CACHE_FIELDS)
            w.writeheader()
            for em in sorted(cache):
                z, at = cache[em]
                w.writerow({"email": em, "zip": z, "checked_at": at})
        if _drive_id():
            from . import drive_io
            drive_io.push(str(CACHE_PATH), file_id=_drive_id())
    except Exception as exc:
        print(f"  residency: cache save failed ({exc}); next run refetches")


def _stale(checked_at: str, today: date) -> bool:
    try:
        return (today - date.fromisoformat(checked_at[:10])).days >= RETRY_EMPTY_DAYS
    except ValueError:
        return True


def attach_zips(ds, emails, today: date | None = None) -> int:
    """Set Climber.zip_code for every climber whose email is in `emails`,
    from cache first, the Beta API for the rest. Returns the number of live
    API fetches. Call with the SMALL candidate set a residency-gated trigger
    is about to consider, never the whole dataset."""
    today = today or date.today()
    want = {(e or "").strip().lower() for e in emails} - {""}
    if not want:
        return 0
    cache = load_cache()
    to_fetch = [e for e in want
                if e not in cache or (not cache[e][0] and _stale(cache[e][1], today))]
    fetched = 0
    if to_fetch:
        try:
            token = mint_token()
        except Exception as exc:
            print(f"  residency: no token ({exc}); "
                  f"{len(to_fetch)} zip(s) stay unknown this run")
            token = None
        if token:
            for e in sorted(to_fetch):
                cache[e] = (lookup_zip(token, e), today.isoformat())
                fetched += 1
            save_cache(cache)
    by_email: dict[str, str] = {e: z for e, (z, _at) in cache.items()}
    for c in ds.climbers.values():
        em = (c.email or "").strip().lower()
        if em in want and by_email.get(em):
            c.zip_code = by_email[em]
    return fetched
