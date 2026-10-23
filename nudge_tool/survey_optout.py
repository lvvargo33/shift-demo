"""Shift-only survey opt-out list (ROADMAP Block 25 A1, 2026-10-23).

Why it exists: Shift's survey emails move to Brevo, and ABC and Shift share ONE
Brevo account. Brevo's own unsubscribe would put the person on that shared
block list and silently stop ABC's emails to them too. So a Shift survey email
carries OUR opt-out link instead: Chris's preference page (borrowed from his
package, survey_page/member_preference.*) on the Shift dashboard, writing to a
list we own. It stops Shift SURVEY emails only. Mailchimp is never touched, so
Shift's marketing keeps going (decision 3, 2026-10-23).

Where the list lives: Firestore, next to the native survey records, because the
dashboard writes it on a click and the send cron reads it each run, and both
already reach Firestore (native_survey._db()). Keyed by EMAIL (sha256 of gym +
email), because every suppression check in our send path is by email. Chris's
own design keys by (identity source, customer id); his collection names are
NOT reused so the two can never collide.

  gyms/{gym}/survey_optouts/{key}                  one per opted-out email
  gyms/{gym}/survey_optouts/{key}/evidence/{id}    one per opt-out request
  gyms/{gym}/survey_optout_capabilities/{cap_id}   one per email sent

Capability token: <64 hex>.<43 urlsafe> (his shape), only its sha256 is
stored, 365-day life (his maximum). An opt-out is sticky: nothing here ever
turns it back off.

  issue_links(...)       SEND time: one capability per email, returns the page
                         URL + the one-click URL (List-Unsubscribe header).
  status(...)            the page's GET: "opted_out" / "none_recorded" / None
                         (bad or expired link). Read-only.
  opt_out(...)           the page's POST and the one-click POST.
  opted_out_emails(...)  the cron's read. RAISES when it cannot read the list
                         (decision 3: an unreadable list aborts the run).
  opted_out_dates(...)   the same read with each opt-out's date (stats push).
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from . import native_survey as island

SCHEMA_VERSION = 1
SCOPE = "survey_optout"
OPTOUTS = "survey_optouts"
CAPS = "survey_optout_capabilities"
LIFETIME = timedelta(days=365)
TOKEN_RE = re.compile(r"[a-f0-9]{64}\.[A-Za-z0-9_-]{43}")
REQUEST_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
PAGE_PATH = "/survey-preference"
ONE_CLICK_PATH = "/survey-preference/one-click"


def page_asset(name: str) -> bytes:
    """One of the page's files (survey_page/survey_optout_page.html/.js/.css),
    for live_server. Only these three names are served."""
    if name not in ("survey_optout_page.html", "survey_optout_page.js",
                    "survey_optout_page.css"):
        raise OptOutError("unknown page file")
    from pathlib import Path
    return (Path(__file__).with_name("survey_page") / name).read_bytes()


class OptOutError(RuntimeError):
    """The list could not be read or written. Messages carry no email."""


def _root(client, db):
    gid = island.gym_id(client)
    island.identifier(gid)
    return gid, db.collection("gyms").document(gid)


def _db_for(client, db):
    if db is not None:
        return db
    if not (island.firestore_ready() and island.gym_id(client)):
        raise OptOutError("Firestore not configured on this service "
                          "(FIRESTORE_PROJECT_ID + key file + gym id)")
    return island._db()


def key_for(gid: str, email: str) -> str:
    return island.digest([gid, "email", (email or "").strip().lower()])


def issue_links(client, email: str, *, db=None, now: datetime | None = None,
                base_url: str | None = None) -> tuple[str, str]:
    """(page URL, one-click URL) for one outgoing email. Writes one capability
    doc. Raises on any failure (the cron then does not send that email)."""
    email = (email or "").strip().lower()
    if not email:
        raise OptOutError("email required")
    base = island.public_base_url() if base_url is None else base_url.rstrip("/")
    if not base:
        raise OptOutError("NATIVE_SURVEY_PUBLIC_BASE_URL not set")
    db = _db_for(client, db)
    gid, root = _root(client, db)
    now = now or datetime.now(timezone.utc)
    cap_id = secrets.token_hex(32)
    token = cap_id + "." + secrets.token_urlsafe(32)
    if not TOKEN_RE.fullmatch(token):
        raise OptOutError("token shape")
    body = {"schema_version": SCHEMA_VERSION, "gym_id": gid, "scope": SCOPE,
            "email": email, "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "issued_at": island.timestamp(now),
            "expires_at": island.timestamp(now + LIFETIME)}
    ref = root.collection(CAPS).document(cap_id)
    # set, not create: the id is 256 random bits, and a retried create whose
    # first attempt landed would fail on its own write
    island._retry(lambda: ref.set(body), island._RETRY_DELAYS_PULL)
    q = urlencode({"token": token})
    return base + PAGE_PATH + "?" + q, base + ONE_CLICK_PATH + "?" + q


def _authorize(client, token, db, now) -> tuple[str, object, dict] | None:
    """(gym id, gym root, capability) for a good token, else None."""
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        return None
    gid, root = _root(client, db)
    snap = island._retry(root.collection(CAPS).document(token.split(".")[0]).get,
                         island._RETRY_DELAYS_MINT)
    if not snap.exists:
        return None
    cap = snap.to_dict() or {}
    want = hashlib.sha256(token.encode()).hexdigest()
    stored = cap.get("token_hash")
    if (cap.get("gym_id") != gid or cap.get("scope") != SCOPE
            or cap.get("schema_version") != SCHEMA_VERSION
            or not isinstance(stored, str) or not hmac.compare_digest(stored, want)
            or not isinstance(cap.get("expires_at"), str)
            or cap["expires_at"] <= island.timestamp(now)
            or not (cap.get("email") or "").strip()):
        return None
    return gid, root, cap


def status(client, token: str, *, db=None, now: datetime | None = None) -> str | None:
    """"opted_out" / "none_recorded" for a good link, None for a bad one.
    Read-only. Raises OptOutError only when Firestore itself is unreachable."""
    db = _db_for(client, db)
    now = now or datetime.now(timezone.utc)
    auth = _authorize(client, token, db, now)
    if auth is None:
        return None
    gid, root, cap = auth
    snap = island._retry(root.collection(OPTOUTS).document(key_for(gid, cap["email"])).get,
                         island._RETRY_DELAYS_MINT)
    return "opted_out" if snap.exists and (snap.to_dict() or {}).get("opted_out") is True \
        else "none_recorded"


def opt_out(client, token: str, request_id: str, *, source: str = "preference_page",
            db=None, now: datetime | None = None) -> str | None:
    """Record the opt-out for the token's email. "opted_out" on success (also
    when it was already recorded), None for a bad link or request id."""
    if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
        return None
    db = _db_for(client, db)
    now = now or datetime.now(timezone.utc)
    auth = _authorize(client, token, db, now)
    if auth is None:
        return None
    gid, root, cap = auth
    email = cap["email"].strip().lower()
    ref = root.collection(OPTOUTS).document(key_for(gid, email))
    cap_id = token.split(".")[0]
    evidence_id = island.digest([SCOPE, cap_id, request_id])
    stamp = island.timestamp(now)
    snap = island._retry(ref.get, island._RETRY_DELAYS_MINT)
    if not (snap.exists and (snap.to_dict() or {}).get("opted_out") is True):
        # sticky: only ever written to True, never back
        ref.set({"schema_version": SCHEMA_VERSION, "gym_id": gid, "email": email,
                 "opted_out": True, "effective_at": stamp, "source": source,
                 "evidence_id": evidence_id})
    ev = ref.collection("evidence").document(evidence_id)
    try:
        ev.create({"schema_version": SCHEMA_VERSION, "gym_id": gid, "source": source,
                   "capability_id": cap_id, "request_id": request_id,
                   "recorded_at": stamp})
    except Exception:  # noqa: BLE001 (AlreadyExists: the same request retried)
        pass
    return "opted_out"


def opted_out_emails(client, *, db=None) -> set[str]:
    """Every email on the Shift survey opt-out list. RAISES OptOutError when
    the list cannot be read: the caller must not send a survey without it."""
    return set(opted_out_dates(client, db=db))


def opted_out_dates(client, *, db=None) -> dict[str, str]:
    """{email: YYYY-MM-DD (UTC) the opt-out took effect} for the whole list.
    The stats push counts an opt-out as an unsubscribe on the person's last
    Brevo survey email before it (session 2, 2026-10-23). RAISES OptOutError
    when the list cannot be read."""
    try:
        db = _db_for(client, db)
        gid, root = _root(client, db)
        snaps = island._retry(lambda: list(root.collection(OPTOUTS).stream()),
                              island._RETRY_DELAYS_PULL)
    except OptOutError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise OptOutError(f"could not read the survey opt-out list "
                          f"({type(exc).__name__})") from exc
    out: dict[str, str] = {}
    for s in snaps:
        d = s.to_dict() or {}
        if d.get("gym_id") == gid and d.get("opted_out") is True:
            e = (d.get("email") or "").strip().lower()
            if e:
                out[e] = str(d.get("effective_at") or "")[:10]
    return out
