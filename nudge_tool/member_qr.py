"""SHIFT QR member survey: the sign at the gym (built 2026-11-05). OURS.

Spec = Luke's 2026-10-29 ruling (ROADMAP `## Decisions 2026-10-29`, QR line),
Chris's 10/26 package as the design reference (not his code):

  scan the sign -> /member-qr -> type the membership email -> the member
  survey page (/member-survey, the same shift_v3 questions, the same
  Firestore records, tokens and save engine as the emailed survey)

  * NO emailed code (overrides the 10/28 ruling). Known risk accepted:
    someone who knows a member's email can answer as that member.
  * Members only: an email that is not a current member gets a polite
    message and no survey.
  * An email shared by several current members gets a "pick your name" screen.
  * One survey per member per 90 days, SHARED with the emailed survey:
      - emailed in the last 90 days and not started -> the QR opens THAT
        emailed survey (same session; nothing saved, so nothing to show)
      - emailed or QR survey already started in the last 90 days -> "we
        already have your feedback", no survey
      - a QR answer counts for the emailed picker's 90 days too
        (answer_dates -> member_survey_native.find(qr_dates=))
  * Never shows past answers: a token is only ever issued into a session
    with nothing saved (native_survey.started() is False).
  * Same member rules as the emailed survey (member_survey_native.find): an
    open adult plan, staff / frozen / no-email held back, 30+ days a member.
    Mailchimp unsubscribes and the survey opt-out list are NOT applied: they
    stop EMAILS, and here the member chose to answer.

Records: a QR survey is a member session whose cycle id is "q" + the date
(YYYYMMDD) of the scan; gyms/{gym}/member_qr/{subject_id} keeps the dates a
QR session was opened for that person, so the 90-day check reads a handful
of documents by id (no queries, no index).

Switch: client.json `member_qr.enabled` (JSON true; anything else = OFF, the
page says the survey is not available). Rate limits are in memory, per web
process (one Render instance): per client IP, per typed email, gym-wide.

Pure of HTTP: live_server's /member-qr routes call decide() and render the
pages in this module.
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path

TRIGGER_NAME = "member_survey"        # the emailed survey's permanent trigger name
CONFIG_KEY = "member_qr"
DEFAULTS = {"enabled": False, "tag": "send_it_member_qr"}
CYCLE_PREFIX = "q"
COLLECTION = "member_qr"
# Short on purpose: a token only ever goes into a session with nothing saved,
# but anyone holding a live token can read what is saved LATER. Two people
# typing the same email within this window could see each other's answers
# (the accepted no-code risk, kept to a small window). 2 hours = time to
# finish the survey at the gym.
TOKEN_LIFETIME = timedelta(hours=2)
PATH = "/member-qr"

# (max, window seconds). Fresh-eyes 11/05: limits that count EVERY try let a
# few hundred junk posts lock real members out for an hour, and gym Wi-Fi
# (one IP) blocked the 16th member. Now only MISSES (an email that is not a
# member, a bad pick) count toward the IP and gym caps, so other people's
# junk can never block a member's own scan.
LIMIT_EMAIL = (12, 3600)        # every try for one typed email (a family of 4 = 8 tries)
LIMIT_IP_MISS = (20, 600)       # misses from one client IP; when full that IP gets "busy"
LIMIT_GYM_MISS = (300, 3600)    # misses gym-wide; when full, misses get "busy", members pass


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------
def load_cfg(client) -> dict:
    """client.json `member_qr` over DEFAULTS. Never raises; anything off
    leaves it OFF and says why in cfg["_problem"]."""
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
    if type(cfg.get("enabled")) is not bool:     # 1 / "true" are not JSON true (fresh-eyes 11/05)
        cfg["_problem"] = f"`enabled` = {blk.get('enabled')!r} must be true or false; left OFF"
        cfg["enabled"] = False
    if not str(cfg.get("tag") or "").startswith("send_it_"):
        cfg["_problem"] = f"`tag` = {blk.get('tag')!r} must start with send_it_; left OFF"
        cfg["enabled"] = False
    return cfg


def is_on(cfg: dict) -> bool:
    return cfg.get("enabled") is True


def cycle_id(d: date) -> str:
    return CYCLE_PREFIX + d.strftime("%Y%m%d")


# --------------------------------------------------------------------------
# rate limits (in memory)
# --------------------------------------------------------------------------
class RateLimiter:
    """allow() before the lookup (per-email tries + an IP that has missed too
    often); miss() after a lookup that found no member; gym_full() = the
    gym-wide miss cap is reached (the caller then answers a miss "busy")."""

    def __init__(self, email=LIMIT_EMAIL, ip_miss=LIMIT_IP_MISS, gym_miss=LIMIT_GYM_MISS,
                 clock=time.monotonic):
        self._lim = {"email": email, "ip": ip_miss, "gym": gym_miss}
        self._hits: dict = {}
        self._lock = threading.Lock()
        self._clock = clock

    def _q(self, kind, key, now):
        n, win = self._lim[kind]
        q = self._hits.setdefault((kind, key), deque())
        while q and now - q[0] >= win:
            q.popleft()
        return q, n

    def allow(self, ip: str, email: str) -> bool:
        """Count one try for this email; False when the email's tries or the
        IP's misses are full (a refused try is not counted)."""
        now = self._clock()
        with self._lock:
            qi, ni = self._q("ip", ip or "?", now)
            if len(qi) >= ni:
                return False
            if email:
                qe, ne = self._q("email", email, now)
                if len(qe) >= ne:
                    return False
                qe.append(now)
            if len(self._hits) > 5000:   # drop idle keys
                for k in [k for k, q in self._hits.items() if not q]:
                    del self._hits[k]
        return True

    def gym_full(self) -> bool:
        with self._lock:
            q, n = self._q("gym", "gym", self._clock())
            return len(q) >= n

    def miss(self, ip: str) -> None:
        now = self._clock()
        with self._lock:
            for kind, key in (("ip", ip or "?"), ("gym", "gym")):
                q, _ = self._q(kind, key, now)
                q.append(now)


LIMITER = RateLimiter()


# --------------------------------------------------------------------------
# who is a member (cached per data-file version)
# --------------------------------------------------------------------------
@dataclass
class Person:
    member: object        # member_survey_native.Member
    status: str           # ok | too_new | held


_index_cache: dict = {}
_index_lock = threading.Lock()


def _stamp(p) -> tuple:
    try:
        st = Path(p).stat()
        return (str(p), st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(p), None, None)


def member_index(client, asof: date) -> dict[str, list[Person]]:
    """email (lower) -> the current members on it, with their QR status.
    Built with the emailed survey's own picker, so the rules cannot drift."""
    from . import member_checkin, member_survey_native as msn
    key = (client.slug, asof, _stamp(client.memberships_csv), _stamp(client.sessions_csv),
           _stamp(Path(client.client_dir) / "client.json"))
    with _index_lock:
        hit = _index_cache.get(client.slug)
        if hit and hit[0] == key:
            return hit[1]
    if not Path(client.memberships_csv).exists():
        raise FileNotFoundError("memberships file missing")
    cfg = msn.load_cfg(client)
    res = msn.find(client.memberships_csv, client.sessions_csv, asof, cfg, log=None,
                   staff_emails=member_checkin.staff_emails_of(client))
    min_days = int(cfg["min_member_days"])
    out: dict[str, list[Person]] = {}

    def add(m, status):
        if m.email:
            out.setdefault(m.email.strip().lower(), []).append(Person(m, status))

    for m in res.eligible:
        add(m, "ok")
    for m in res.young:
        add(m, "too_new")
    for m in res.held:
        if m.skip == "email shared with another member":
            add(m, "ok" if (asof - m.since).days >= min_days else "too_new")
        else:
            add(m, "held")      # frozen, staff, unsubscribed: no survey here
    for lst in out.values():
        lst.sort(key=lambda p: (p.member.name.lower(), p.member.climber_id))
    with _index_lock:
        _index_cache[client.slug] = (key, out)
    return out


# --------------------------------------------------------------------------
# the decision
# --------------------------------------------------------------------------
@dataclass
class Outcome:
    kind: str                 # survey | pick | not_member | not_due | already | off | busy | error
    token: str = ""
    choices: list = field(default_factory=list)   # [(index, label)] for "pick"
    note: str = ""            # for the server log only (never an email)


def pick_key(climber_id: str) -> str:
    """Opaque, stable value for the pick button (no climber id on the page)."""
    import hashlib
    return hashlib.sha256(("member-qr-pick:" + str(climber_id)).encode()).hexdigest()[:12]


def _label(m) -> str:
    first = (m.first_name or m.name.split(" ")[0] or "Member").strip()
    last = m.name[len(first):].strip() if m.name.lower().startswith(first.lower()) else ""
    return first + (f" {last[0].upper()}." if last else "")


def _session(db, client, customer_id: str, cyc: str):
    from . import native_survey as ns
    from .survey_page import native_survey as core
    ctx = core.GymContext(ns.gym_id(client))
    source = ns.identifier(str(ns.native_cfg(client).get("source") or "beta"))
    sid = core.occurrence_id(ctx, source, ns.identifier(customer_id), "member", ns.identifier(cyc))
    root = db.collection("gyms").document(ctx.gym_id)
    snap = ns._retry(root.collection("sessions").document(sid).get, ns._RETRY_DELAYS_MINT)
    return ns._check(snap.to_dict(), ctx.gym_id, "session") if snap.exists else None


def _qr_ref(db, client, customer_id: str):
    from . import native_survey as ns
    from .survey_page import native_survey as core
    ctx = core.GymContext(ns.gym_id(client))
    source = ns.identifier(str(ns.native_cfg(client).get("source") or "beta"))
    subj = core.subject_id(ctx, source, ns.identifier(customer_id))
    return db.collection("gyms").document(ctx.gym_id).collection(COLLECTION).document(subj)


def decide(client, email: str, pick: str = "", *, log, asof: date | None = None,
           db=None, now: datetime | None = None, index=None) -> Outcome:
    """What the sign does for this typed email (+ the picked name, when one
    email has several members). Raises on Firestore / data trouble; the
    caller shows the "try again" page."""
    from . import member_survey_native as msn
    from . import native_survey as ns
    cfg = load_cfg(client)
    if not is_on(cfg):
        return Outcome("off")
    now = now or datetime.now(timezone.utc)
    asof = asof or date.today()
    email = (email or "").strip().lower()
    if not email or "@" not in email or len(email) > 254:
        return Outcome("not_member", note="not an email")
    people = (index if index is not None else member_index(client, asof)).get(email) or []
    if not people:
        return Outcome("not_member", note="no current member")
    if len(people) > 1:
        # the pick value is a fixed key per member (not a list position), so a
        # data refresh between the two screens can never land on someone else
        choices = [(pick_key(p.member.climber_id), _label(p.member)) for p in people]
        if not pick:
            return Outcome("pick", choices=choices)
        person = next((p for p in people if pick_key(p.member.climber_id) == pick), None)
        if person is None:
            return Outcome("pick", choices=choices, note="bad pick")
    else:
        person = people[0]
    m = person.member
    if person.status != "ok":
        return Outcome("not_due", note=person.status)

    ms_cfg = msn.load_cfg(client)
    cooldown = timedelta(days=int(ms_cfg["cooldown_days"]))
    if db is None:
        if not ns.mint_enabled(client):
            raise RuntimeError("native survey not enabled on this service")
        db = ns._db()

    # 1. an emailed survey in the last 90 days (outreach log, by inbox; a
    #    shared inbox is never emailed, so this is this member's)
    sent = sorted(d[:10] for d in log.sent_dates(m.email, TRIGGER_NAME) if len(d) >= 10)
    recent = [d for d in sent if asof - date.fromisoformat(d) < cooldown]
    if recent:
        s = _session(db, client, m.climber_id, msn_cycle(recent[-1]))
        if s is None:
            return Outcome("not_due", note="emailed, no session found")
        if ns.started(s):
            return Outcome("already", note="emailed survey started")
        tok = ns.issue_member_token(client, m.email, m.climber_id, msn_cycle(recent[-1]),
                                    str(cfg["tag"]), db=db, now=now, lifetime=TOKEN_LIFETIME)
        return Outcome("survey", token=tok, note="opened the emailed survey")

    # 2. QR surveys opened in the last 90 days
    ref = _qr_ref(db, client, m.climber_id)
    snap = ns._retry(ref.get, ns._RETRY_DELAYS_MINT)
    days = sorted(str(d) for d in ((snap.to_dict() or {}).get("opened") or [])) if snap.exists else []
    today = asof.isoformat()
    for d in reversed(days):
        try:
            dd = date.fromisoformat(d)
        except ValueError:
            continue
        if asof - dd >= cooldown:
            break
        s = _session(db, client, m.climber_id, cycle_id(dd))
        if s is not None and ns.started(s):
            return Outcome("already", note="QR survey started " + d)

    # 3. today's QR session (reused if this person scanned earlier today and
    #    saved nothing). Checked directly too, so a lost "opened" write can
    #    never hand out a token into saved answers.
    if today not in days:
        s = _session(db, client, m.climber_id, cycle_id(asof))
        if s is not None and ns.started(s):
            return Outcome("already", note="QR survey started today (not in opened list)")
    tok =ns.issue_member_token(client, m.email, m.climber_id, cycle_id(asof), str(cfg["tag"]),
                                db=db, now=now, lifetime=TOKEN_LIFETIME,
                                context=dict(m.context, channel="qr"))
    if today not in days:
        ns._retry(lambda: ref.set({"gym_id": ns.gym_id(client), "opened": sorted(set(days + [today])),
                                   "updated_at": now.isoformat()}, merge=True),
                  ns._RETRY_DELAYS_MINT)
    return Outcome("survey", token=tok, note="new QR survey")


def msn_cycle(day: str) -> str:
    from .native_survey import member_cycle_id
    return member_cycle_id(date.fromisoformat(day[:10]))


# --------------------------------------------------------------------------
# for the emailed picker (send_nudges) and the reply screen
# --------------------------------------------------------------------------
def is_qr_session(session: dict) -> bool:
    return str(session.get("cycle_id") or "").startswith(CYCLE_PREFIX)


def qr_state(client, *, db=None, now: datetime | None = None) -> tuple[dict[str, str], set[str]]:
    """(answered, pending) for the emailed picker.
    answered = {climber_id: latest "YYYY-MM-DD"} of QR member surveys with
    anything saved (they count for the 90 days).
    pending = emails holding a QR survey with nothing saved yet and a token
    still alive: today's run must not email them, or they could save the QR
    one after the email went out = two surveys in 90 days (fresh-eyes 11/05).
    They simply wait a day. Raises on a Firestore failure (the caller holds
    member surveys)."""
    from . import native_survey as ns
    if db is None:
        db = ns._db()
    now = now or datetime.now(timezone.utc)
    stamp = ns.timestamp(now)
    gid = ns.gym_id(client)
    root = db.collection("gyms").document(gid)
    sessions = [s.to_dict() or {} for s in ns._retry(
        lambda: list(root.collection("sessions").stream()), ns._RETRY_DELAYS_PULL)]
    messages = [m.to_dict() or {} for m in ns._retry(
        lambda: list(root.collection("messages").stream()), ns._RETRY_DELAYS_PULL)]
    by_sid = {m.get("session_id"): m for m in messages}
    out: dict[str, str] = {}
    pending: set[str] = set()
    for s in sessions:
        if s.get("gym_id") != gid or s.get("survey_type") != "member" or not is_qr_session(s):
            continue
        m = by_sid.get(s.get("id")) or {}
        if not ns.started(s):
            live = any(str((c or {}).get("expires_at") or "") > stamp
                       for c in (m.get("capabilities") or {}).values())
            if live and m.get("contact_email"):
                pending.add(str(m["contact_email"]).strip().lower())
            continue
        cid = str(m.get("customer_id") or "")
        day = str(s.get("cycle_id"))[1:]
        if not cid or len(day) != 8 or not day.isdigit():
            continue
        iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        if iso > out.get(cid, ""):
            out[cid] = iso
    return out, pending


def answer_dates(client, *, db=None) -> dict[str, str]:
    return qr_state(client, db=db)[0]


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------
GYM = "Shift"
_CSS = """
:root{--ink:#1d2321;--muted:#5b6461;--accent:#173b34;--bg:#f3f1e8;--card:#fff;--line:#d9d6cb}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:30rem;margin:0 auto;padding:10vh 16px 3rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:1.5rem}
h1{font-size:1.45rem;line-height:1.25;margin:0 0 .5rem}
p{margin:.5rem 0;color:var(--muted)}
label{display:block;font-weight:600;margin:1.1rem 0 .35rem}
input[type=email]{width:100%;font:inherit;padding:.75rem .85rem;border:1px solid #b9b5a8;border-radius:10px;background:#fff}
input[type=email]:focus{outline:3px solid #9cc3b6;border-color:var(--accent)}
button{display:block;width:100%;margin-top:1rem;font:inherit;font-weight:600;padding:.8rem;border:0;border-radius:10px;background:var(--accent);color:#fff;cursor:pointer}
button.pick{background:#fff;color:var(--accent);border:2px solid var(--accent);margin-top:.6rem}
.small{font-size:.9rem}
"""


def _page(title: str, body: str) -> bytes:
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<meta name=\"robots\" content=\"noindex\">"
            f"<title>{escape(title)}</title><style>{_CSS}</style></head>"
            f"<body><main><div class=\"card\">{body}</div></main></body></html>").encode("utf-8")


def page_form(error: str = "") -> bytes:
    err = f"<p role=\"alert\">{escape(error)}</p>" if error else ""
    return _page(f"{GYM} member survey", (
        f"<h1>Your experience matters.</h1>"
        f"<p>{GYM} members: share your feedback and help us make the gym better.</p>{err}"
        f"<form method=\"post\" action=\"{PATH}\">"
        "<label for=\"email\">The email on your membership</label>"
        "<input id=\"email\" name=\"email\" type=\"email\" required autocomplete=\"email\" "
        "inputmode=\"email\" autocapitalize=\"none\" spellcheck=\"false\" maxlength=\"254\">"
        "<button type=\"submit\">Start the survey</button></form>"))


def page_pick(email: str, choices) -> bytes:
    buttons = "".join(
        f"<button class=\"pick\" type=\"submit\" name=\"pick\" value=\"{escape(i)}\">{escape(label)}</button>"
        for i, label in choices)
    return _page(f"{GYM} member survey", (
        "<h1>Which member are you?</h1>"
        "<p>More than one member uses this email. Pick your name.</p>"
        f"<form method=\"post\" action=\"{PATH}\">"
        f"<input type=\"hidden\" name=\"email\" value=\"{escape(email, quote=True)}\">"
        f"{buttons}</form>"))


MESSAGES = {
    "not_member": ("We couldn't find a membership for that email.",
                   f"This survey is for current {GYM} members. Check the email on your membership "
                   "and try again, or ask at the front desk.", True),
    "not_due": ("Thanks for stopping by!",
                "You're not due for a member survey right now. We'll ask again later.", False),
    "already": ("Thanks, we already have your feedback.",
                "Members can share one survey every 90 days. We'll ask again later.", False),
    "off": ("The member survey isn't open right now.", "Please check back soon.", False),
    "busy": ("Too many tries.", "Please wait a few minutes and try again.", True),
    "error": ("Something went wrong on our end.", "Please try again in a minute.", True),
}


def page_message(kind: str) -> bytes:
    head, text, retry = MESSAGES.get(kind, MESSAGES["error"])
    again = f"<p class=\"small\"><a href=\"{PATH}\">Try again</a></p>" if retry else ""
    return _page(f"{GYM} member survey",
                 f"<h1>{escape(head)}</h1><p>{escape(text)}</p>{again}")
