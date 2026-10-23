"""The one send boundary for Shift's survey emails (ROADMAP Block 25 A1,
2026-10-23). Adapted from Chris's nudge_tool/survey_dispatch.py
(shift-survey-main-integration-2026-10-21.zip, patch 0001): same config shape,
same three routes, same emergency stop, same rule that a native failure never
falls back to Mailchimp. His production blockers (application_for() always
None) are replaced by our Brevo path, survey_mail.send().

    client.json  survey.dispatch = {"ftv": "legacy", "emergency_stop": false,
                                    "warmup": {"start": "YYYY-MM-DD", "days": 14,
                                               "per_day": 25}}

  legacy  Mailchimp tag, exactly today's behavior (and the default: a missing
          "dispatch" block or a missing "ftv" key = legacy).
  native  Brevo send from our copy of the live email (survey_mail).
  hold    send nothing; the person stays in the queue for the next run.
  emergency_stop true (or a broken dispatch block) = every survey email held.

Only the survey triggers route (OPERATIONS). Every other trigger (offers,
reengage, nudges) is tagged in Mailchimp as always, whatever this says.

Every real send path uses it: send_nudges.py (cron), live_server.send_now
(dashboard Send button), cli.py --live. One left on Mailchimp would double-send
after the flip.

Suppression for native sends (decision 3, 2026-10-23):
  Mailchimp unsubscribed / cleaned   read again here (the CLI goes on without
                                     it; tester 2026-10-23)
  Shift survey opt-out list          survey_optout (our page); also honored on
                                     legacy once a dispatch block exists, so a
                                     switch back never re-mails an opt-out
  Brevo blocks                       hard bounce, spam complaint, admin block,
                                     plus an unsubscribe whose sender is Shift's
                                     From address (Brevo adds its own
                                     List-Unsubscribe to every transactional
                                     email). An unsubscribe from an ABC email on
                                     the shared account does not count.
prepare() reads every list BEFORE anything is sent and raises DispatchError if
it cannot (the caller aborts the run: an unreadable list never means "send").

Warm-up cap (decision 8; session 2, 2026-10-23): "native" needs a warmup
block. Through start + days - 1 (and before start) at most per_day NEW Brevo
survey emails go out per day, counted from the outreach log's esp=brevo rows
dated today plus this run's own sends; the rest are "held" and stay in the
queue for the next run. After the last warm-up day there is no cap. A native
route with no (or a broken) warmup block refuses to send (DispatchError), so
the go-live commit cannot forget it.

Every Brevo send (and every "already sent" restore) sets target.esp = "brevo"
so the outreach log row says which service sent it (stats push, session 2); a
restore also sets target.sent_date to the day the email really went out. A
send Brevo never answered ("UNCERTAIN") counts against the cap: it may have
gone out (tester round, 2026-10-23).

Send ledger (Firestore gyms/<gym>/survey_sends, one doc per email + trigger):
reserved before the Brevo call, sent after it. A "sent" doc means no second
email even if the outreach log lost the row (the log row is restored); a doc
left "reserved" (Brevo never answered) is NOT retried automatically, because
the email may have gone out. Mailchimp's "enters a journey once" did this job
on the legacy path.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta

from dotenv import load_dotenv

OPERATIONS = {
    "survey_request": ("ftv", "invitation"),
    "survey_reminder": ("ftv", "reminder"),
}
ENTRYPOINTS = frozenset({"scheduler", "dashboard", "cli"})
ROUTES = ("legacy", "native", "hold")
# Brevo /smtp/blockedContacts reason codes that mean "this address cannot or
# must not get mail from anyone on the account".
DELIVERY_BLOCKS = frozenset({"hardBounce", "contactFlaggedAsSpam", "adminBlocked"})


class DispatchError(RuntimeError):
    """Native sends cannot go ahead (config, credentials or a list read)."""


@dataclass(frozen=True)
class Outcome:
    transport: str   # "mailchimp" | "brevo" | "none"
    state: str       # submitted | held | opted_out | blocked | failed
    detail: str = ""
    sent_date: str = ""   # a ledger restore: the date the email really went out

    @property
    def sent(self) -> bool:
        return self.state == "submitted"


def dispatch_cfg(client) -> dict:
    return (getattr(client, "survey", None) or {}).get("dispatch", {})


def routing(client, program: str) -> str:
    """Chris's routing(): anything malformed holds; ftv defaults to legacy."""
    config = dispatch_cfg(client)
    if not isinstance(config, dict) or config.get("emergency_stop", False) is not False:
        return "hold"
    route = config.get(program, "legacy" if program == "ftv" else "hold")
    return route if route in ROUTES else "hold"


def route_for(client, target) -> str | None:
    """The route for one send target, or None when it is not a survey email."""
    op = OPERATIONS.get(getattr(target, "trigger_name", ""))
    if op is None:
        return None
    return routing(client, op[0])


def brevo_settings(client):
    """Shift's Brevo identity from its env file: BREVO_API_KEY + BREVO_SEND_FROM
    required (no silent fallback to the Brevo login, a personal Gmail);
    BREVO_SEND_FROM_NAME defaults to client_name; BREVO_REPLY_TO to the
    send-from address. Same env names as ABC's services."""
    from .config import Settings
    load_dotenv(client.env_file, override=True)
    key = os.getenv("BREVO_API_KEY", "").strip()
    frm = os.getenv("BREVO_SEND_FROM", "").strip()
    missing = [n for n, v in (("BREVO_API_KEY", key), ("BREVO_SEND_FROM", frm)) if not v]
    if missing:
        raise DispatchError(f"Brevo survey sends need {', '.join(missing)} on this service")
    return Settings(api_key=key, esp="brevo", sender_email=frm,
                    sender_name=(os.getenv("BREVO_SEND_FROM_NAME", "").strip()
                                 or client.client_name),
                    reply_to_email=os.getenv("BREVO_REPLY_TO", "").strip() or frm)


@dataclass
class Dispatcher:
    """Build once per run, prepare(targets) once, then send(t) per target."""
    client: object
    mc: object                       # MailchimpClient (tag_member)
    entrypoint: str
    db: object = None                # Firestore client (tests pass a fake)
    brevo: object = None             # BrevoClient (tests pass a fake)
    now: object = None
    base_url: str | None = None
    optouts: set = field(default_factory=set)
    mc_suppressed: set = field(default_factory=set)
    blocked: set = field(default_factory=set)
    prepared: bool = False
    cap: int | None = None           # warm-up per-day limit today (None = no cap)
    brevo_today: int = 0             # Brevo survey emails already out today
    # the date the caller writes on its outreach log rows (the CLI uses the
    # data's last day, not today); the cap counts rows dated either day
    asof: date | None = None

    def __post_init__(self):
        if self.entrypoint not in ENTRYPOINTS:
            raise ValueError("unknown dispatch entrypoint")

    def native_count(self, targets) -> int:
        return sum(1 for t in targets if route_for(self.client, t) == "native")

    def prepare(self, targets) -> list[str]:
        """Read what this run's survey sends need, BEFORE anything is sent.
        Returns report lines. Raises DispatchError on anything missing.

          no survey.dispatch block   nothing read (today's state: the run is
                                     byte-for-byte the old Mailchimp path)
          block present, legacy      the Shift opt-out list, so an opt-out
                                     made while on Brevo still holds after a
                                     switch back (tester 2026-10-23)
          native                     the opt-out list + Mailchimp's
                                     unsubscribed/cleaned list (read here, not
                                     trusted from the caller: the CLI carries
                                     on without it) + Brevo's block list"""
        self.prepared = True
        lines: list[str] = []
        cfg_present = "dispatch" in ((getattr(self.client, "survey", None) or {}))
        if cfg_present and not isinstance(dispatch_cfg(self.client), dict):
            lines.append("  survey dispatch: survey.dispatch is not a {...} block; "
                         "EVERY survey email is HELD until it is fixed")
        surveys = [t for t in targets if route_for(self.client, t) is not None]
        n = self.native_count(targets)
        n_legacy = sum(1 for t in surveys if route_for(self.client, t) == "legacy")
        if not n and not (cfg_present and n_legacy):
            return lines
        from . import native_survey, survey_optout
        if n:
            if not native_survey.enabled(self.client):
                raise DispatchError("survey.native.enabled is off in client.json, so the "
                                    "embed emails' survey links cannot be made")
            if self.db is None:
                if not native_survey.mint_enabled(self.client):
                    raise DispatchError("Firestore env (FIRESTORE_PROJECT_ID + key) or "
                                        "NATIVE_SURVEY_PUBLIC_BASE_URL missing on this service")
                self.db = native_survey._db()
            if not (native_survey.public_base_url() if self.base_url is None else self.base_url):
                raise DispatchError("NATIVE_SURVEY_PUBLIC_BASE_URL not set on this service")
        try:
            self.optouts = survey_optout.opted_out_emails(self.client, db=self.db)
        except Exception as exc:  # noqa: BLE001
            raise DispatchError(f"could not read the Shift survey opt-out list ({exc})") from exc
        if not n:
            lines.append(f"  survey dispatch: legacy (Mailchimp); "
                         f"{len(self.optouts)} on the Shift survey opt-out list (skipped)")
            return lines
        if self.brevo is None:
            from .brevo_client import BrevoClient
            self.brevo = BrevoClient(brevo_settings(self.client), self.client)
        try:
            self.mc_suppressed = {e.strip().lower() for e in self.mc.suppressed_emails()}
        except Exception as exc:  # noqa: BLE001
            raise DispatchError(f"could not read Mailchimp's unsubscribed list ({exc})") from exc
        try:
            rows = self.brevo.blocked_contacts()
        except Exception as exc:  # noqa: BLE001
            raise DispatchError(f"could not read Brevo's block list ({exc})") from exc
        self.cap, cap_line = self._warmup()
        own = (getattr(getattr(self.brevo, "s", None), "sender_email", "") or "").strip().lower()
        self.blocked = {r["email"] for r in rows if _blocks_shift(r, own)}
        ignored = sum(1 for r in rows if not _blocks_shift(r, own))
        lines.append(f"  survey dispatch: {n} survey email(s) route to Brevo; "
                     f"{len(self.optouts)} on the Shift survey opt-out list, "
                     f"{len(self.mc_suppressed)} Mailchimp unsubscribed/cleaned, "
                     f"{len(self.blocked)} Brevo block(s) "
                     f"({ignored} other Brevo block(s) ignored: not Shift's)")
        lines.append(cap_line)
        return lines

    def _today(self) -> date:
        # the same clock the callers date their outreach log rows with
        return self.now.date() if self.now is not None else date.today()

    def _warmup(self) -> tuple[int | None, str]:
        """(per-day cap for today or None, report line). Raises DispatchError
        when the warmup block is missing or malformed (decision 8)."""
        w = dispatch_cfg(self.client).get("warmup")
        if not isinstance(w, dict):
            raise DispatchError("survey.dispatch.warmup {start, days, per_day} is required "
                                "for Brevo survey sends (decision 8: 25/day for 14 days)")
        try:
            start = date.fromisoformat(str(w["start"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise DispatchError(f"survey.dispatch.warmup start is not a YYYY-MM-DD date ({exc})") from exc
        days, per_day = w.get("days"), w.get("per_day")
        # whole numbers only (true / 2.9 / "14" refused), both written out,
        # inside sane bounds (Brevo's free plan is 300/day for both gyms)
        for name, v, hi in (("days", days, 366), ("per_day", per_day, 300)):
            if type(v) is not int or not 1 <= v <= hi:
                raise DispatchError(f"survey.dispatch.warmup {name} must be a whole number "
                                    f"from 1 to {hi} (got {v!r})")
        today, last = self._today(), start + timedelta(days=days - 1)
        if today > last:
            return None, f"  survey dispatch: Brevo warm-up ended {last.isoformat()}; no daily cap"
        from . import outreach
        stamps = {today.isoformat()} | ({self.asof.isoformat()} if self.asof else set())
        self.brevo_today = sum(1 for r in outreach.load_rows(self.client)
                               if (r.get("esp") or "").strip().lower() == "brevo"
                               and (r.get("sent_date") or "").strip()[:10] in stamps
                               and (r.get("mode") or "").strip() != "test")
        return per_day, (f"  survey dispatch: Brevo warm-up cap {per_day}/day through "
                         f"{last.isoformat()}; {self.brevo_today} already sent today")

    def send(self, t) -> Outcome:
        """Send one target down its route. Mailchimp errors propagate exactly
        as before (the callers already catch them); a native failure returns
        state "failed" and never touches Mailchimp."""
        route = route_for(self.client, t)
        email = (t.email or "").strip().lower()
        if route is not None and route != "hold" and self.prepared and email in self.optouts:
            return Outcome("none", "opted_out")
        if route is None or route == "legacy":
            self.mc.tag_member(t.email, t.first_name, t.tag)
            return Outcome("mailchimp", "submitted")
        if route == "hold":
            return Outcome("none", "held", "survey.dispatch says hold")
        if not self.prepared:
            raise DispatchError("prepare() must run before a native send")
        if email in self.mc_suppressed:
            return Outcome("none", "unsubscribed", "Mailchimp unsubscribed/cleaned")
        if email in self.blocked:
            return Outcome("none", "blocked", "Brevo block")
        if self.cap is not None and self.brevo_today >= self.cap:
            return Outcome("none", "held", f"Brevo warm-up cap of {self.cap}/day reached; "
                           "stays queued for the next run")
        o = self._send_native(t, email)
        if o.transport == "brevo" and not o.sent_date:
            self.brevo_today += 1     # a real new email (a restore sent nothing)
        elif o.detail.startswith("UNCERTAIN"):
            self.brevo_today += 1     # Brevo may have sent it: it uses up the cap
        if o.transport == "brevo":
            try:
                t.esp = "brevo"
                if o.sent_date:
                    # the restored log row keeps the day it really went out, so
                    # its Brevo events pin and it never counts as today's send
                    t.sent_date = o.sent_date
            except AttributeError:
                pass
        return o

    def _send_native(self, t, email: str) -> Outcome:
        """Build, reserve, send, record. The send ledger (Firestore
        survey_sends, one doc per email + trigger) is what stops a duplicate
        email when the outreach log write is lost after a send (Mailchimp's
        "enters a journey once" did that job before; tester 2026-10-23)."""
        from . import survey_mail
        try:
            ref, prior = _ledger(self.client, self.db, email, t.trigger_name)
        except Exception as exc:  # noqa: BLE001
            return Outcome("none", "failed", f"send ledger unavailable ({type(exc).__name__})")
        if prior.get("state") == "sent":
            day = str(prior.get("sent_at", ""))[:10]
            return Outcome("brevo", "submitted",
                           f"already sent {day}; no new email, outreach log row restored",
                           sent_date=day or _now_iso(self.now)[:10])
        if prior.get("state") == "reserved":
            return Outcome("none", "failed",
                           "an earlier attempt got no answer from Brevo, so it may have "
                           "gone out; NOT retried. Check Brevo's log for this address, "
                           f"then delete Firestore gyms/<gym>/survey_sends/{ref.id}")
        try:
            subject, html, tags, headers = survey_mail.build(
                self.client, t, db=self.db, now=self.now, base_url=self.base_url)
        except Exception as exc:  # noqa: BLE001 (no fallback, by design)
            return Outcome("none", "failed", f"{type(exc).__name__}: {exc}")
        try:
            ref.set({"schema_version": 1, "gym_id": _gid(self.client), "email": email,
                     "trigger": t.trigger_name, "tag": t.tag, "state": "reserved",
                     "reserved_at": _now_iso(self.now)})
        except Exception as exc:  # noqa: BLE001
            return Outcome("none", "failed", f"send ledger unavailable ({type(exc).__name__})")
        try:
            self.brevo.send_html(t.email, t.first_name, subject, html, tags=tags,
                                 headers=headers)
        except Exception as exc:  # noqa: BLE001
            if isinstance(getattr(exc, "status", None), int):
                # a BrevoError: Brevo answered with an error status (or the
                # client refused before sending): nothing went out, retry is safe
                _safe(lambda: ref.set({"state": "failed", "failed_at": _now_iso(self.now)},
                                      merge=True))
                return Outcome("none", "failed", f"{type(exc).__name__}: {exc}")
            # no answer (timeout, dropped connection): it may have gone out
            return Outcome("none", "failed", f"UNCERTAIN, may have been sent; not retried "
                           f"({type(exc).__name__}: {exc})")
        _safe(lambda: ref.set({"state": "sent", "sent_at": _now_iso(self.now)}, merge=True))
        return Outcome("brevo", "submitted")


def _blocks_shift(row: dict, own_sender: str) -> bool:
    """A Brevo block that should stop a SHIFT survey: a delivery block from
    anyone, or an unsubscribe from a Shift email (Brevo adds its own
    List-Unsubscribe header to every transactional email and records the
    sender on the block). An unsubscribe from an ABC email does not count."""
    reason = row.get("reason") or ""
    if reason in DELIVERY_BLOCKS:
        return True
    sender = (row.get("sender") or "").strip().lower()
    return reason.startswith("unsubscribed") and bool(own_sender) and sender == own_sender


def _gid(client) -> str:
    from . import native_survey
    return native_survey.gym_id(client)


def _now_iso(now) -> str:
    from datetime import datetime, timezone
    from . import native_survey
    return native_survey.timestamp(now or datetime.now(timezone.utc))


def _ledger(client, db, email: str, trigger: str):
    from . import native_survey
    gid = _gid(client)
    key = native_survey.digest([gid, "survey_send", email, trigger])
    ref = db.collection("gyms").document(gid).collection("survey_sends").document(key)
    snap = native_survey._retry(ref.get, native_survey._RETRY_DELAYS_PULL)
    return ref, (snap.to_dict() or {}) if snap.exists else {}


def _safe(fn) -> None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        print(f"  survey dispatch: send ledger update FAILED ({type(exc).__name__}); "
              "the email went out, the outreach log still records it")
