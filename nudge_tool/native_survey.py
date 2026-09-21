"""Native survey island (ROADMAP_SPEC_2026-09.md Block 11, built 2026-09-21).

Chris's survey page + a Firestore database replace Google Forms as the ANSWER
BOX for the FTV survey. Our tool keeps deciding and sending exactly as today.
Three things live here:

  mint(...)           CLICK TIME, called from live_server's /s redirect: create
                      the Firestore records his page needs (subject + session +
                      message + capability token) and return the page URL. Any
                      failure returns "" and the caller redirects into the
                      Google Form exactly as before. The 11 AM cron never mints.
  refresh_cache(...)  EACH MORNING, from the send cron only (NATIVE_SURVEY_PULL=1):
                      pull completed sessions out of Firestore, write them to
                      _native_survey_cache.csv and push that to Drive. Fail-soft:
                      a failed pull keeps yesterday's file and prints a FAILED
                      line (CLAUDE.md rule 5).
  load_cache(...)     the dashboard and the stats push read the CSV only. They
                      never talk to Firestore.

survey.load_responses merges the cache with the Form sheet (native wins per
email); only a `completed` session counts as answered, so partials still get
the reminder.

Record shape, id derivation and the capability token are COPIED from Chris's
patch 0001 (nudge_tool/native_survey.py, native_survey_definitions.py,
native_survey_store.py in shift-native-survey-handoff.zip, 2026-09-20) so our
records validate on his page. Do NOT tidy the dataclasses or the id helpers:
Definition.fingerprint is sha256(json(asdict(Definition))) and must equal his
byte for byte (the page rejects a session whose definition_hash differs). The
bundle itself stays unapplied (decision 2026-09-21).

Switches (all off = every function is a no-op, which is ABC's state):
  client.json survey.native.enabled     master switch for READING the cache
  FIRESTORE_PROJECT_ID                  + a key file = Firestore reachable
  GOOGLE_APPLICATION_CREDENTIALS or
  NATIVE_SURVEY_SA_KEY                  service-account JSON path (Render Secret File)
  NATIVE_SURVEY_GYM_ID                  optional override of survey.native.gym_id
  FIRESTORE_DATABASE_ID                 optional, default "(default)"
  NATIVE_SURVEY_PUBLIC_BASE_URL         Chris's public page origin; needed to mint
  NATIVE_SURVEY_CACHE_DRIVE_ID          Drive file id of the cache CSV (unset =
                                        local file only)
  NATIVE_SURVEY_PULL                    "1" on the send cron only: refresh, don't
                                        just read
"""
from __future__ import annotations

import csv
import hashlib
import http.client
import json
import os
import re
import secrets
import ssl
import time
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from .config import ClientConfig

# --------------------------------------------------------------------------
# Copied from Chris's nudge_tool/native_survey.py (patch 0001). Verbatim where
# it matters for ids and hashes.
# --------------------------------------------------------------------------
SCHEMA_VERSION = 1


class SurveyError(ValueError):
    """Invalid request; messages deliberately omit answer and identity values."""


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise SurveyError("invalid identifier")
    return value


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def timestamp(now: datetime) -> str:
    if now.tzinfo is None:
        raise SurveyError("timezone required")
    return now.astimezone(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class GymContext:
    """Construct ONLY from trusted server configuration, never request fields."""
    gym_id: str

    def __post_init__(self):
        identifier(self.gym_id)


def subject_id(ctx: GymContext, source: str, customer_id: str) -> str:
    identifier(source)
    return digest([ctx.gym_id, identifier(customer_id)])


def occurrence_id(ctx: GymContext, source: str, customer_id: str,
                  survey_type: str, cycle_id: str | None = None) -> str:
    key = [ctx.gym_id, identifier(source), identifier(customer_id)]
    if survey_type == "ftv" and cycle_id is None:
        return digest(key + ["first_visit"])
    if survey_type == "member" and cycle_id:
        return digest(key + ["member", identifier(cycle_id)])
    raise SurveyError("invalid survey occurrence")


@dataclass(frozen=True)
class Condition:
    question: str
    operator: str                 # eq, contains, lte
    value: str | int


@dataclass(frozen=True)
class Question:
    id: str
    wording: str
    kind: str                    # rating, single, multi, text
    required: bool = True
    version: int = 1
    minimum: int = 0
    maximum: int = 10
    options: tuple[tuple[str, str], ...] = ()
    visible_if: Condition | None = None
    max_length: int = 4000


@dataclass(frozen=True)
class Definition:
    id: str
    version: int
    survey_type: str
    questions: tuple[Question, ...]

    @property
    def fingerprint(self) -> str:
        return digest(asdict(self))


# Chris's nudge_tool/native_survey_definitions.py FTV_V1, verbatim (wording is
# his; the option codes are what his page stores and what pull() maps back).
FTV_V1 = Definition("ftv", 1, "ftv", (
    Question("q1", "How was your first visit overall?", "rating", minimum=1, maximum=5,
             options=(("1", "Awful"), ("5", "Exceptional"))),
    Question("q2", "How likely is it that climbing could become something you do regularly?", "single",
             options=(("unlikely", "Unlikely"), ("not_sure", "Not sure"), ("likely", "Likely"))),
    Question("q3", "Main issue, if any?", "single", options=(
        ("too_expensive", "Too expensive"), ("too_crowded", "Too crowded"),
        ("too_hard", "Climbing felt too hard"), ("intimidating", "It felt intimidating"),
        ("front_desk", "Front desk experience"),
        ("confusing", "Confusing / didn't know where to start"),
        ("routes_not_fun", "The routes weren't fun"), ("no_issues", "No issues"))),
    Question("q4", "Want to tell us more?", "text", required=False),
))


def applicability(definition: Definition, answers: dict) -> dict[str, bool]:
    """FTV_V1 has no conditional questions, so this is all-True; kept in his
    shape so the stored session matches new_session() in patch 0001."""
    visible = {}
    for q in definition.questions:
        c = q.visible_if
        applies = True
        if c:
            parent = answers.get(c.question, {})
            applies = visible.get(c.question, False) and parent.get("status") == "answered"
            if applies:
                value = parent["value"]
                applies = ((value == c.value) if c.operator == "eq" else
                           (c.value in value) if c.operator == "contains" else
                           (value <= c.value))
        visible[q.id] = bool(applies)
    return visible


def new_session(ctx, source, customer_id, definition, now, cycle_id=None):
    sid = occurrence_id(ctx, source, customer_id, definition.survey_type, cycle_id)
    return {
        "schema_version": SCHEMA_VERSION, "gym_id": ctx.gym_id, "id": sid,
        "subject_id": subject_id(ctx, source, customer_id),
        "survey_type": definition.survey_type, "cycle_id": cycle_id,
        "definition_id": definition.id, "definition_version": definition.version,
        "definition_hash": definition.fingerprint, "dispatch_owner": "native",
        "answers": {}, "applicability": applicability(definition, {}),
        "revision": 0, "state": "unanswered", "created_at": timestamp(now),
        "started_at": None, "last_saved_at": None, "completed_at": None,
        "completion": None, "assignment": None,
    }


def new_subject(ctx, source, customer_id, sid):
    """The subject doc create_session() writes in patch 0001 (native_survey_store)."""
    return {
        "schema_version": SCHEMA_VERSION, "gym_id": ctx.gym_id,
        "id": sid, "identity_source": identifier(source),
        "customer_id": identifier(customer_id), "assignments": {},
        "feedback_suppression": {"suppressed": False, "source": None},
        "member_cycles": {}, "dispatch_reservation": None,
    }


def message_id(session_id: str, kind: str = "survey_invitation", policy_id=None, ordinal=None):
    if kind == "survey_invitation" and policy_id is None and ordinal is None:
        return digest([session_id, "initial"])
    if kind == "survey_reminder" and policy_id and type(ordinal) is int and ordinal > 0:
        return digest([session_id, identifier(policy_id), ordinal])
    raise SurveyError("invalid message identity")


def new_invitation(session, template_id, template_version, now):
    identifier(template_id)
    if type(template_version) is not int or template_version < 1:
        raise SurveyError("invalid template version")
    return {"schema_version": SCHEMA_VERSION, "gym_id": session["gym_id"],
            "id": message_id(session["id"]), "session_id": session["id"],
            "subject_id": session["subject_id"], "type": "survey_invitation",
            "assignment": deepcopy(session["assignment"]), "template_id": template_id,
            "template_version": template_version, "state": "planned",
            "planned_at": timestamp(now), "due_at": None, "deadline_at": None,
            "provider": {"name": None, "message_id": None, "requested_at": None,
                         "accepted_at": None, "sent_at": None, "delivered_at": None,
                         "failed_at": None, "uncertain_at": None},
            "attempts": [], "capabilities": {}}


# Capability lifetime his issue_capability() uses by default (max 90 days).
CAPABILITY_LIFETIME = timedelta(days=30)
# His pilot cap: a message holds at most 32 capabilities, none ever revoked.
CAPABILITY_LIMIT = 32
# What his _validate_token accepts: <64 hex>.<43 urlsafe chars>.
TOKEN_RE = re.compile(r"[a-f0-9]{64}\.[A-Za-z0-9_-]{43}")

# --------------------------------------------------------------------------
# Our side: switches, client, retries.
# --------------------------------------------------------------------------
CACHE_FIELDS = ["email", "answered_at", "q1", "q2_raw", "q3_raw", "q4",
                "session_id", "completed_at_utc", "pulled_at"]
CACHE_NAME = "_native_survey_cache.csv"

_RETRY_DELAYS_PULL = (1, 2, 4, 8)   # the cron can wait (rule 1: 5 attempts)
_RETRY_DELAYS_MINT = (1,)           # a click cannot: 2 attempts, then the Form
_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    TimeoutError, ConnectionError, ssl.SSLError, http.client.HTTPException, OSError,
)


def native_cfg(client: ClientConfig) -> dict:
    return dict(((getattr(client, "survey", None) or {}).get("native")) or {})


def _truthy(v) -> bool:
    """True for True / 1 / "true" / "yes" / "on"; a string "false" is False."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def enabled(client: ClientConfig) -> bool:
    """client.json says the island is on for this gym (the READ switch). With
    this false nothing here runs: ABC's state until Chris's ABC page exists."""
    sv = getattr(client, "survey", None) or {}
    return _truthy(sv.get("enabled")) and _truthy(native_cfg(client).get("enabled"))


def _key_path() -> str:
    return (os.getenv("NATIVE_SURVEY_SA_KEY") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()


def firestore_ready() -> bool:
    """The env this service needs to reach Firestore at all."""
    return bool(os.getenv("FIRESTORE_PROJECT_ID", "").strip()) and bool(_key_path())


def gym_id(client: ClientConfig) -> str:
    return (os.getenv("NATIVE_SURVEY_GYM_ID") or native_cfg(client).get("gym_id") or "").strip()


def public_base_url() -> str:
    return os.getenv("NATIVE_SURVEY_PUBLIC_BASE_URL", "").strip().rstrip("/")


def mint_enabled(client: ClientConfig) -> bool:
    """Everything a click-time mint needs. Missing any piece = Form fallback."""
    return enabled(client) and firestore_ready() and bool(gym_id(client)) and bool(public_base_url())


def pull_requested() -> bool:
    return _truthy(os.getenv("NATIVE_SURVEY_PULL") or "")


_db_cache = None


def _db():
    """One Firestore client per process (lazy import: the library is only
    installed where the island runs; ABC never imports it)."""
    global _db_cache
    if _db_cache is not None:
        return _db_cache
    from google.cloud import firestore
    from google.oauth2 import service_account
    creds = service_account.Credentials.from_service_account_file(
        _key_path(), scopes=["https://www.googleapis.com/auth/datastore"])
    _db_cache = firestore.Client(
        project=os.getenv("FIRESTORE_PROJECT_ID", "").strip(),
        database=os.getenv("FIRESTORE_DATABASE_ID", "").strip() or "(default)",
        credentials=creds)
    return _db_cache


def _retryable() -> tuple[type[BaseException], ...]:
    """Transport blips plus the Firestore statuses worth a second try."""
    try:
        from google.api_core import exceptions as gexc
        api = (gexc.ServiceUnavailable, gexc.DeadlineExceeded, gexc.Aborted,
               gexc.InternalServerError, gexc.ResourceExhausted)
    except Exception:
        api = ()
    return _TRANSPORT_ERRORS + api


def _retry(fn, delays):
    kinds = _retryable()
    for delay in delays:
        try:
            return fn()
        except kinds:
            time.sleep(delay)
    return fn()


def _check(doc: dict, gid: str, what: str) -> dict:
    """His _checked(): gym + schema must match before we trust a record."""
    if doc.get("gym_id") != gid:
        raise SurveyError(f"{what}: gym mismatch")
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise SurveyError(f"{what}: unsupported record schema")
    return doc


def _template_id(tag: str) -> str:
    """The message's template_id must be one of his identifiers. The Mailchimp
    tag on the link nearly is one (live_server also allows dots, which become
    underscores here); anything else becomes 'external'."""
    t = (tag or "").strip().replace(".", "_")
    return t if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", t) else "external"


# --------------------------------------------------------------------------
# mint: click time.
# --------------------------------------------------------------------------
def mint(client: ClientConfig, email: str, climber_id: str, q1: int | None = None,
         tag: str = "", *, db=None, now: datetime | None = None,
         base_url: str | None = None) -> str:
    """Create (or reuse) this climber's FTV session on Firestore, add a fresh
    capability to its invitation message, and return the page URL. "" on ANY
    failure so the caller falls back to the Google Form. Never raises.

    Idempotent where his page needs it: the session and message ids are
    deterministic (sha256 of gym + customer id), so a second click reuses both
    and only adds a capability (his issue_capability() does the same; earlier
    tokens stay valid). An existing session is never overwritten."""
    try:
        if db is None:
            if not mint_enabled(client):
                return ""
            db = _db()
        base = public_base_url() if base_url is None else base_url.rstrip("/")
        if not base:
            return ""
        cfg = native_cfg(client)
        ctx = GymContext(gym_id(client))
        source = identifier(str(cfg.get("source") or "beta"))
        customer_id = identifier(str(climber_id or "").strip())
        email = (email or "").strip().lower()
        if not email:
            raise SurveyError("email required")
        if q1 is not None and (type(q1) is not int or not 1 <= q1 <= 5):
            raise SurveyError("invalid embedded Q1 candidate")
        now = now or datetime.now(timezone.utc)
        root = db.collection("gyms").document(ctx.gym_id)

        session = new_session(ctx, source, customer_id, FTV_V1, now)
        sref = root.collection("sessions").document(session["id"])
        snap = _retry(sref.get, _RETRY_DELAYS_MINT)
        if snap.exists:
            session = _check(snap.to_dict(), ctx.gym_id, "session")
            if session.get("definition_hash") != FTV_V1.fingerprint:
                raise SurveyError("session: definition changed without version bump")
        else:
            pref = root.collection("subjects").document(session["subject_id"])
            ps = _retry(pref.get, _RETRY_DELAYS_MINT)
            if ps.exists:
                subject = _check(ps.to_dict(), ctx.gym_id, "subject")
                if subject.get("identity_source") != source:
                    raise SurveyError("subject: identity source mismatch")
            else:
                pref.set(new_subject(ctx, source, customer_id, session["subject_id"]))
            try:
                sref.create(session)
            except Exception:  # noqa: BLE001 (AlreadyExists: a second click won the race)
                again = _retry(sref.get, _RETRY_DELAYS_MINT)
                if not again.exists:
                    raise
                session = _check(again.to_dict(), ctx.gym_id, "session")

        message = new_invitation(session, _template_id(tag), 1, now)
        # Our additions, mirroring what his dispatcher writes on a message it
        # sends (patch 0001 lines 645-647): who this went to and who sent it.
        message.update({"dispatch_owner": "native", "contact_email": email,
                        "identity_source": source, "customer_id": customer_id})
        mref = root.collection("messages").document(message["id"])
        ms = _retry(mref.get, _RETRY_DELAYS_MINT)
        if ms.exists:
            message = _check(ms.to_dict(), ctx.gym_id, "message")
        else:
            try:
                mref.create(message)
            except Exception:  # noqa: BLE001 (same race on the message)
                again = _retry(mref.get, _RETRY_DELAYS_MINT)
                if not again.exists:
                    raise
                message = _check(again.to_dict(), ctx.gym_id, "message")
        if len(message.get("capabilities") or {}) >= CAPABILITY_LIMIT:
            raise SurveyError("pilot capability limit reached")

        secret = secrets.token_urlsafe(32)
        token = message["id"] + "." + secret
        if not TOKEN_RE.fullmatch(token):
            raise SurveyError("token shape")
        hashed = hashlib.sha256(token.encode()).hexdigest()
        cap = {"gym_id": ctx.gym_id, "session_id": session["id"],
               "message_id": message["id"], "scope": "survey", "candidate": q1,
               "issued_at": timestamp(now),
               "expires_at": timestamp(now + CAPABILITY_LIFETIME)}
        mref.set({"capabilities": {hashed: cap}}, merge=True)
        return base + "/survey?" + urlencode({"token": token})
    except Exception as exc:  # noqa: BLE001 (a click must never 500)
        print(f"  native survey: mint FAILED ({type(exc).__name__}: {exc}); Form fallback")
        return ""


# --------------------------------------------------------------------------
# pull + cache: each morning (cron), read-only for everyone else.
# --------------------------------------------------------------------------
def _s(v) -> str:
    """A string no matter what Firestore hands back (a datetime becomes ISO)."""
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.isoformat()
    return v if isinstance(v, str) else str(v)


_tz_warned: set = set()


def _local_date(iso_utc, tz_name: str) -> str:
    """His timestamps are ISO-8601 UTC. The sheet wants the gym's local date."""
    s = _s(iso_utc).strip()
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s[:10]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        dt = dt.astimezone(ZoneInfo(tz_name or "UTC"))
    except Exception:  # noqa: BLE001 (unknown zone or no tz database)
        if tz_name not in _tz_warned:
            _tz_warned.add(tz_name)
            print(f"  native survey: timezone '{tz_name}' unknown, dates fall back to UTC")
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d")


def _answer(answers: dict, q: str):
    a = answers.get(q) if isinstance(answers, dict) else None
    if isinstance(a, dict) and a.get("status") == "answered":
        return a.get("value")
    return None


def pull(client: ClientConfig, *, db=None, now: datetime | None = None) -> list[dict]:
    """Completed FTV sessions for this gym as cache rows (q2/q3 already mapped
    to the same raw text the Form writes, so blocker_map / intent_map apply).
    Full collection scans, like his reader; hundreds of docs, fine. Raises on
    a Firestore failure (refresh_cache turns that into a FAILED line)."""
    cfg = native_cfg(client)
    gid = gym_id(client)
    if not gid:
        raise SurveyError("gym_id not configured")
    tz_name = cfg.get("timezone") or "UTC"
    maps = cfg.get("map") or {}
    q2_map = maps.get("q2") or {}
    q3_map = maps.get("q3") or {}
    if db is None:
        db = _db()
    root = db.collection("gyms").document(gid)
    sessions = _retry(lambda: list(root.collection("sessions").stream()), _RETRY_DELAYS_PULL)
    messages = _retry(lambda: list(root.collection("messages").stream()), _RETRY_DELAYS_PULL)

    # email lives on the message, not the session; newest planned message wins.
    # One odd document must never take the whole pull down (tester 2026-09-21):
    # each is read under its own guard, skipped and counted.
    bad = 0
    email_by_session: dict[str, tuple[str, str]] = {}
    for m in messages:
        try:
            d = m.to_dict() or {}
            sid, em, at = d.get("session_id"), d.get("contact_email"), d.get("planned_at")
            if d.get("gym_id") != gid or not sid or em in (None, ""):
                continue
            if (not isinstance(sid, str) or not isinstance(em, str)
                    or not isinstance(at, (str, datetime, type(None)))):
                raise TypeError("message fields")
            stamp = _s(at)
            em = em.strip().lower()
            if em and (sid not in email_by_session or stamp > email_by_session[sid][0]):
                email_by_session[sid] = (stamp, em)
        except Exception:  # noqa: BLE001
            bad += 1

    def code_text(v, table: dict) -> str:
        if v is None:
            return ""
        key = v if isinstance(v, str) else _s(v)
        return table.get(key, key)

    pulled_at = timestamp(now or datetime.now(timezone.utc))
    rows: list[dict] = []
    for s in sessions:
        try:
            d = s.to_dict() or {}
            if (d.get("gym_id") != gid or d.get("survey_type") != "ftv"
                    or d.get("state") != "completed"):
                continue
            completion = d.get("completion") or {}
            answers = completion.get("answers") if isinstance(completion, dict) else None
            if not isinstance(answers, dict):
                continue
            q1, q2, q3, q4 = (_answer(answers, q) for q in ("q1", "q2", "q3", "q4"))
            completed_at = _s(completion.get("completed_at") or d.get("completed_at"))
            sid = _s(d.get("id"))
            rows.append({
                "email": email_by_session.get(sid, ("", ""))[1],
                "answered_at": _local_date(completed_at, tz_name),
                "q1": "" if q1 is None else _s(q1),
                "q2_raw": code_text(q2, q2_map),
                "q3_raw": code_text(q3, q3_map),
                "q4": q4 if isinstance(q4, str) else "",
                "session_id": sid,
                "completed_at_utc": completed_at,
                "pulled_at": pulled_at,
            })
        except Exception:  # noqa: BLE001
            bad += 1
    if bad:
        print(f"  native survey: {bad} malformed document(s) skipped")
    rows.sort(key=lambda r: (r["completed_at_utc"], r["session_id"]))
    return rows


def cache_path(client: ClientConfig | None = None) -> Path:
    """Repo root (nudge_tool's parent), next to _allgyms_engagement_cache.csv.
    Gitignored in the bundle (it holds emails). client.json
    survey.native.cache_path overrides it (tests, local runs)."""
    override = (native_cfg(client).get("cache_path") if client is not None else "") or ""
    if override:
        p = Path(override)
        return p if p.is_absolute() else Path(__file__).resolve().parent.parent / p
    return Path(__file__).resolve().parent.parent / CACHE_NAME


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return [{k: (r.get(k) or "") for k in CACHE_FIELDS} for r in csv.DictReader(f)]


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CACHE_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CACHE_FIELDS})


def _drive_id() -> str:
    return os.getenv("NATIVE_SURVEY_CACHE_DRIVE_ID", "").strip()


_last_drive_pull = 0.0
_DRIVE_TTL = int(os.getenv("NATIVE_SURVEY_CACHE_TTL", "300") or "300")


def _drive_pull(path: Path, *, force: bool = False) -> None:
    """Fetch the cache from Drive when an id is set; throttled so a dashboard
    that renders every few minutes does not hit Drive every time. Fail-soft."""
    global _last_drive_pull
    fid = _drive_id()
    if not fid:
        return
    now = time.monotonic()
    if not force and _last_drive_pull and (now - _last_drive_pull) < _DRIVE_TTL:
        return
    try:
        from . import drive_io
        drive_io.pull(fid, str(path))
        _last_drive_pull = now
    except Exception as exc:  # noqa: BLE001
        print(f"  native survey: cache pull from Drive FAILED ({exc}); using the local file")


_refreshed_rows: list[dict] | None = None


def refresh_cache(client: ClientConfig, *, db=None, path: Path | None = None,
                  now: datetime | None = None) -> list[dict]:
    """Cron path: pull Firestore -> rewrite the cache -> push to Drive. Once per
    process (load_responses runs several times in a cron run). Prints the
    greppable report line `native survey: N completed, M new`. On a failed pull
    prints `native survey: pull FAILED (...)` and returns yesterday's rows."""
    global _refreshed_rows
    if _refreshed_rows is not None and path is None:
        return _refreshed_rows
    path = path or cache_path(client)
    _drive_pull(path, force=True)
    try:
        old = _read_csv(path)
    except Exception as exc:  # noqa: BLE001 (a bad file must not stop the cron)
        print(f"  native survey: yesterday's cache unreadable ({type(exc).__name__}: {exc}); "
              f"treating it as empty")
        old = []
    if db is None and not firestore_ready():
        print(f"  native survey: pull skipped (Firestore not configured); "
              f"keeping the cached {len(old)} row(s)")
        return old
    try:
        rows = pull(client, db=db, now=now)
    except Exception as exc:  # noqa: BLE001 (rule 5: loud line, run continues)
        print(f"  native survey: pull FAILED ({type(exc).__name__}: {exc}); "
              f"keeping yesterday's cache ({len(old)} row(s))")
        if path == cache_path(client):
            _refreshed_rows = old   # do not retry on every load_responses this run
        return old
    _write_csv(path, rows)
    fid = _drive_id()
    if fid:
        try:
            from . import drive_io
            drive_io.push(str(path), file_id=fid, svc=drive_io._service(drive_io._RW))
        except Exception as exc:  # noqa: BLE001
            print(f"  native survey: cache push to Drive FAILED ({exc}); local file written")
    new = {r["session_id"] for r in rows} - {r["session_id"] for r in old}
    print(f"  native survey: {len(rows)} completed, {len(new)} new")
    if path == cache_path(client):
        _refreshed_rows = rows
    return rows


def load_cache(client: ClientConfig, *, path: Path | None = None) -> list[dict]:
    """Read path for the dashboard / stats push: Drive (throttled) then the
    local CSV. Empty list on any error, never raises."""
    try:
        path = path or cache_path(client)
        _drive_pull(path)
        return _read_csv(path)
    except Exception as exc:  # noqa: BLE001
        print(f"  native survey: cache read FAILED ({exc}); no native rows this run")
        return []


def responses(client: ClientConfig) -> list[dict]:
    """What survey.load_responses asks for: refreshed rows on the cron
    (NATIVE_SURVEY_PULL=1), the cached rows everywhere else. [] when off."""
    if not enabled(client):
        return []
    if pull_requested():
        return refresh_cache(client)
    return load_cache(client)


def reset_for_tests() -> None:
    """Forget the per-process memo + Drive throttle (verify scripts only)."""
    global _refreshed_rows, _last_drive_pull, _db_cache
    _refreshed_rows = None
    _last_drive_pull = 0.0
    _db_cache = None
