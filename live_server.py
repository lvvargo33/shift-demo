"""Live dashboard server (ADDITIVE — touches nothing in the cron/static pipeline).

Why this exists: the deployed /live page is a STATIC file rebuilt only when the
GitHub Actions cron fires, so a fresh Form submission can sit unseen for 10-20+
min (or overnight). This little server fixes that by re-running the SAME
nudge_tool engine on EVERY page load and reading the live Form sheet right then,
so "submit -> refresh -> it's there in a few seconds."

It only READS: the Beta CSV exports + the Google Form response sheet (via the
same survey.apply path the rest of the tool uses). It never writes to Mailchimp,
never touches the Form, the response sheet, the cloud_deploy repo, GitHub, or
Cloudflare. The existing cron+static demo keeps running untouched as the backup.

Run:
    py live_server.py                 # http://127.0.0.1:8000  (client=shift_demo_live)
    py live_server.py --port 8000 --client shift_demo_live

Per-request query overrides (handy mid-demo):
    /?asof=2026-06-09     pin the as-of date
    /?days=6              as-of = today + N days (default 6, so a fresh
                          submission both LANDS and ROUTES to its nudge)
    /?client=shift_demo   show the offline fake-data walkthrough instead
    /?refresh=8           auto-reload the page every 8s (resets to Dashboard tab)
    /healthz              plain-text health check
"""
from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from nudge_tool import config, email_results, engine, gym_metrics, ingest, livesend, outreach, survey
# A2 step 4 (2026-11-02): ABC's Members views, fed by the native member survey
from nudge_tool import (member_followups, member_reply, member_reply_edits,  # noqa: E402
                        member_responses_native, member_survey, member_views)
from nudge_tool.config import DASHBOARD_TEMPLATE, load_client, load_settings

DEFAULT_CLIENT = "shift_demo_live"
DEFAULT_DAYS_AHEAD = 6  # as-of = today + 6 so a same-day submission routes to its nudge

# Real Beta data lives in a private Drive folder (never in git). On the host,
# set these to the Drive file ids (drive_sync.py push prints them) and the
# server pulls fresh CSVs to the client's data paths before each render. Unset
# (the local-dev default) = read the existing local CSVs, behavior unchanged.
_DRIVE_IDS = {
    "transactions_csv": os.getenv("BETA_DRIVE_TX"),
    "sessions_csv": os.getenv("BETA_DRIVE_SES"),
    "memberships_csv": os.getenv("BETA_DRIVE_MEM"),
}
# The outreach log (the send record that powers the Recovery Queue export) lives
# in the same private Drive folder. The send_nudges cron pushes it after each
# run; the web service pulls it read-only so the export shows current sends.
_OUTREACH_DRIVE_ID = os.getenv("OUTREACH_DRIVE_ID")
_DRIVE_TTL = int(os.getenv("BETA_DRIVE_TTL", "60") or "60")  # min seconds between pulls
_last_pull = 0.0


def _ensure_beta_data(client) -> None:
    """If Drive file ids are configured, pull fresh CSVs to the client's data
    paths (read-only scope, TTL-throttled). No-op when no ids are set."""
    global _last_pull
    if not any(_DRIVE_IDS.values()) and not _OUTREACH_DRIVE_ID:
        return
    now = time.monotonic()
    if _last_pull and (now - _last_pull) < _DRIVE_TTL:
        return
    from nudge_tool import drive_io
    svc = drive_io._service(drive_io._RO)
    pulled = []
    for attr, fid in _DRIVE_IDS.items():
        if not fid:
            continue
        dest = str(getattr(client, attr))
        drive_io.pull(fid, dest, svc=svc)
        pulled.append(attr.replace("_csv", ""))
    if _OUTREACH_DRIVE_ID:
        drive_io.pull(_OUTREACH_DRIVE_ID, str(client.outreach_log), svc=svc)
        pulled.append("outreach_log")
    # A2 step 4: member-reply state, same fail-soft pulls as ABC's live_server
    # (MEMBER_FOLLOWUPS_DRIVE_ID / MEMBER_REPLY_EDITS_DRIVE_ID, both optional)
    for name, mod in (("member_followups", member_followups),
                      ("member_reply_edits", member_reply_edits)):
        try:
            if mod.pull(client, svc=svc):
                pulled.append(name)
        except Exception as e:  # noqa: BLE001
            print(f"  ({name} pull failed: {e})")
    _last_pull = now
    print(f"  pulled from Drive: {', '.join(pulled)}")

# Optional login gate (protects the real PII on the hosted /live page). When
# LIVE_USER + LIVE_PASS env vars are set, every request must pass HTTP Basic
# Auth. Unset (the local-dev default) = no gate, exactly as before. This is the
# app-level fallback; a fronting Cloudflare Access policy can gate it instead.
BASIC_USER = os.getenv("LIVE_USER")
BASIC_PASS = os.getenv("LIVE_PASS")
# Per-person logins (10/14, Jadee + Isaac): env LIVE_PASS_<NAME> = that person's
# password, and <name> (any case) is their username. Deleting one env var locks
# out just that person; the shared LIVE_USER/LIVE_PASS login is unchanged.
NAMED_LOGINS = {k[len("LIVE_PASS_"):].lower(): v for k, v in os.environ.items()
                if k.upper().startswith("LIVE_PASS_") and len(k) > len("LIVE_PASS_") and v}
AUTH_ON = bool((BASIC_USER and BASIC_PASS) or NAMED_LOGINS)

# Auto-reload interval (seconds) baked into every page so the bare URL refreshes
# itself (handy for a "watch it land" demo without appending ?refresh=). 0 = off
# (the local default). A per-request ?refresh=N still overrides; ?refresh=0 off.
# A full-page reload interrupts whoever is reading, so keep the baked-in interval
# gentle: never auto-reload faster than every 3 minutes, even if a smaller value
# is configured. (Explicit ?refresh=N is left alone for ad-hoc "watch it land" tests.)
MIN_AUTO_REFRESH = 180
_raw_refresh = int(os.getenv("LIVE_REFRESH", "0") or "0")
DEFAULT_REFRESH = max(_raw_refresh, MIN_AUTO_REFRESH) if _raw_refresh > 0 else 0


def _default_days(slug: str) -> int:
    """How many days ahead of today the as-of date sits, by default.

    The real `shift` client serves the TRUE current operational queue, so it
    uses today (0). The fake-data demo clients use +6 so a just-submitted Form
    response jumps straight to its day-5..7 nudge during a live pitch. Override
    globally with the LIVE_DAYS_AHEAD env, or per-request with ?days=/?asof=."""
    env = os.getenv("LIVE_DAYS_AHEAD")
    if env not in (None, ""):
        return int(env)
    return 0 if slug == "shift" else DEFAULT_DAYS_AHEAD


def _resolve_asof(params: dict, slug: str) -> date:
    """As-of date for the day-windows. ?asof= wins; else today + ?days (per-client default)."""
    if params.get("asof"):
        return date.fromisoformat(params["asof"][0])
    days = int(params["days"][0]) if params.get("days") else _default_days(slug)
    return date.today() + timedelta(days=days)


def _truthy(v) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def _send_enabled(client) -> bool:
    """True only when live sending is switched on (NUDGE_LIVE_ENABLED env or the
    client config) AND Mailchimp send creds are present. Drives the dashboard
    banner and whether the Send buttons (POST /send) actually tag anyone."""
    if not (_truthy(os.getenv("NUDGE_LIVE_ENABLED")) or getattr(client, "live_enabled", False)):
        return False
    try:
        load_settings(client, require=True)
        return True
    except Exception:
        return False


def _push_outreach_log(client) -> None:
    """Push the updated outreach log back to its Drive file id so the daily cron
    and the next page load both see a manual send. No id (local dev) = skip."""
    fid = os.getenv("OUTREACH_DRIVE_ID")
    if not fid or not client.outreach_log.exists():
        return
    from nudge_tool import drive_io
    drive_io.push(str(client.outreach_log), file_id=fid, svc=drive_io._service(drive_io._RW))


# --- Mailchimp engagement activity (Insights offer/survey slides) -----------
# Opens/clicks for everyone in the outreach log, fetched in a background thread
# (one small read per contact) so page renders never block on Mailchimp. The
# render uses the latest completed snapshot; None until the first fetch lands,
# and the slides then hide the open/click bars instead of showing a false 0.
_ACTIVITY_REFRESH_SECS = int(os.getenv("MC_ACTIVITY_TTL", "900") or "900")
_activity_map: dict | None = None
_journey_ids: set | None = None       # campaign_ids that are journey/automation emails
_campaign_type_cache: dict = {}       # campaign_id -> Mailchimp campaign type
_activity_lock = threading.Lock()
# Unsubscribed + cleaned addresses, refreshed on the same pass (Block 15 S9,
# 2026-09-24): the page's queue used to be built with no unsubscribe list, so
# an unsubscribed climber showed as "queued" while the Send button (which
# reads the list itself) would refuse them. None until the first read lands.
_unsub_set: set | None = None


def _refresh_activity_once(client_slug: str) -> None:
    global _activity_map, _journey_ids, _unsub_set
    client = load_client(client_slug)
    try:
        settings = load_settings(client, require=True)
    except Exception:
        return  # no Mailchimp creds on this host -> the bars just stay hidden
    try:
        _ensure_beta_data(client)  # freshest outreach log (TTL-throttled)
    except Exception:
        pass
    from nudge_tool.mailchimp_client import MailchimpClient, MailchimpError
    from requests.exceptions import RequestException
    emails = sorted({
        (r.get("email") or "").strip().lower()
        for r in outreach.load_rows(client)
        if (r.get("mode") or "").strip() != "test" and (r.get("email") or "").strip()
    })
    mc = MailchimpClient(settings)
    # the unsubscribe list first (one list read) and published at once, so a
    # restart shows it within seconds, not after the whole sweep below
    try:
        unsub = mc.suppressed_emails()
        with _activity_lock:
            _unsub_set = unsub
    except Exception as exc:  # noqa: BLE001 (display only: never cost the activity pass)
        print(f"  unsubscribe list read failed (keeping the last one): {exc}")
    fetched: dict = {}
    for e in emails:
        try:
            fetched[e] = mc.member_activity(e)
        except (MailchimpError, RequestException, RuntimeError):
            prev = (_activity_map or {}).get(e)
            if prev is not None:
                fetched[e] = prev  # keep the last good read for this contact
    # Classify every campaign seen in the feeds once (cached across refreshes)
    # so the engine can pin opens to journey emails and ignore newsletter
    # blasts that went out the same day as a nudge.
    cids = {a.get("campaign_id")
            for ev in fetched.values() for a in ev or []
            if a.get("campaign_id")}
    for cid in sorted(cids - set(_campaign_type_cache)):
        try:
            _campaign_type_cache[cid] = mc.campaign_type(cid)
        except (MailchimpError, RequestException, RuntimeError):
            pass  # unknown this round; retried next refresh
    journeys = {cid for cid, t in _campaign_type_cache.items()
                if t == "automation-email"}
    with _activity_lock:
        _activity_map = fetched
        _journey_ids = journeys


def _activity_refresher(client_slug: str) -> None:
    while True:
        try:
            _refresh_activity_once(client_slug)
        except Exception as e:
            print(f"  activity refresh failed (will retry): {e}")
        time.sleep(_ACTIVITY_REFRESH_SECS)


_send_lock = threading.Lock()


def send_now(client_slug: str, req: dict) -> dict:
    """Manually tag the requested climbers in Mailchimp (a Journey then sends),
    record the sends in the outreach log, and push the log back to Drive.

    Safe by construction: it only sends to people still ELIGIBLE in today's
    freshly-built queue, so it can never reach someone who already returned,
    already got this message, or first visited before the go-live floor. A lock
    serializes sends so two quick clicks can't double-tag. `req["ids"]` limits the
    send to those climber ids; omit it to send the whole eligible queue."""
    with _send_lock:
        client = load_client(client_slug)
        if not (_truthy(os.getenv("NUDGE_LIVE_ENABLED")) or getattr(client, "live_enabled", False)):
            return {"ok": False, "reason": "not_enabled",
                    "message": "Sending isn’t switched on for this account yet."}
        try:
            settings = load_settings(client, require=True)
        except Exception:
            return {"ok": False, "reason": "no_creds",
                    "message": "Email sending isn’t set up on this server yet. "
                               "Add the Mailchimp keys on the host, then try again."}

        from nudge_tool.mailchimp_client import MailchimpClient, MailchimpError
        from requests.exceptions import RequestException

        # Refresh the same data the dashboard reads (Beta CSVs + outreach log).
        _ensure_beta_data(client)
        ds = ingest.load(client)
        survey_result = survey.apply(ds, client)
        log = outreach.load(client)
        outreach.tighten_survey_sent(ds, log)

        # Never tag a suppressed/unsubscribed email; if we can't read that list, abort.
        try:
            unsub = MailchimpClient(settings).suppressed_emails()
        except (RuntimeError, MailchimpError, RequestException):
            return {"ok": False, "reason": "no_unsub",
                    "message": "Couldn’t reach Mailchimp to check unsubscribes, so "
                               "nothing was sent. Try again in a moment."}

        asof = date.today()
        suppressed_out: list = []
        queue = engine.build_queue(ds, client, asof, log=log, unsubscribed=unsub,
                                   suppressed_out=suppressed_out, dedup_email=True,
                                   own_email_guard=True)
        targets = livesend.derive_sends(queue)

        ids = req.get("ids")
        if ids is not None:
            idset = {str(i) for i in ids}
            targets = [t for t in targets if t.climber_id in idset]

        mc = MailchimpClient(settings)
        # Block 25 A1: survey emails go through the dispatch switch, same as
        # the cron. An unreadable opt-out / Brevo block list = send nothing.
        from nudge_tool.survey_dispatch import Dispatcher, DispatchError
        disp = Dispatcher(client, mc, entrypoint="dashboard", asof=asof)
        try:
            disp.prepare(targets)
        except DispatchError as e:
            print(f"  /send: survey dispatch not ready ({e}); nothing sent")
            return {"ok": False, "reason": "survey_dispatch",
                    "message": "Survey emails couldn’t be got ready (setup or an "
                               "opt-out list check failed), so nothing was sent. "
                               "Try again in a moment; if it keeps happening, check "
                               "the server log."}
        sent: list = []
        failed: list = []
        for t in targets:
            try:
                o = disp.send(t)
            except (MailchimpError, RequestException) as e:
                failed.append({"name": t.name, "email": t.email, "error": str(e)})
                continue
            if o.sent:
                sent.append(t)
            elif o.state == "failed":
                failed.append({"name": t.name, "email": t.email, "error": o.detail})
            else:
                failed.append({"name": t.name, "email": t.email,
                               "error": f"not sent ({o.state}"
                                        + (f": {o.detail}" if o.detail else "") + ")"})
        if sent:
            outreach.append(client, livesend.log_rows(sent, asof))
            try:
                _push_outreach_log(client)
            except Exception:
                pass  # local log recorded it; Mailchimp "enters once" stops a dup email
        return {
            "ok": True,
            "sent": [{"name": t.name, "email": t.email, "tag": t.tag} for t in sent],
            "failed": failed,
        }


def render(client_slug: str, asof: date) -> str:
    """Run the engine fresh and return the self-contained dashboard HTML string.

    Mirrors engine.write_dashboard but returns the string instead of writing a
    file, and skips the Mailchimp status read (like --skip-status) so a page load
    is fast and works even if creds/network are unavailable."""
    client = load_client(client_slug)
    _ensure_beta_data(client)                          # pull real CSVs from Drive (host only)
    ds = ingest.load(client)
    survey_result = survey.apply(ds, client)          # reads the live Form sheet
    log = outreach.load(client)
    outreach.tighten_survey_sent(ds, log)

    with _activity_lock:
        activity = _activity_map
        journeys = _journey_ids
        unsub = set(_unsub_set or ())
    suppressed_out: list = []
    queue = engine.build_queue(ds, client, asof, log=log,
                               unsubscribed=unsub, suppressed_out=suppressed_out,
                               dedup_email=True, own_email_guard=True)
    generated_at = datetime.now().isoformat(timespec="seconds")
    payload = engine.build_payload(ds, queue, client, asof, "dry-run",
                                   generated_at, survey_result, suppressed_out,
                                   sent_rows=outreach.load_rows(client),
                                   activity_by_email=activity,
                                   journey_campaign_ids=journeys,
                                   email_results=email_results.snapshot(client_slug))

    payload["send_enabled"] = _send_enabled(client)
    # Home "Gym" card (Block 23). Uses the real Eastern date, not `asof` (which
    # runs DEFAULT_DAYS_AHEAD days ahead here), and never raises.
    payload["gym"] = gym_metrics.build_beta(client.memberships_csv, client.transactions_csv,
                                            gym_metrics.eastern_today())

    # A2 step 4: the Members section (ABC parity). Only when client.json has a
    # member_survey block (member_views.wants_block); answers are read from
    # Firestore by member_responses_native. Never takes the FTV page down.
    if member_views.wants_block(client) and _members_visible(client):
        try:
            payload["members"] = member_views.build_live(
                client, asof, outreach_rows=outreach.load_rows(client))
            # Brevo is SHIFT's reply path, not the Mailchimp creds behind
            # send_enabled: the page's Send button follows this instead
            payload["members"]["config"]["reply_send_enabled"] = _reply_send_ready(client)
        except Exception:  # noqa: BLE001
            tb = traceback.format_exc()
            print("  ERROR building members block:\n" + tb)
            payload["members"] = {"as_of": asof.isoformat(), "error": tb.strip().splitlines()[-1],
                                  "responses": [], "waves": [], "dims": [],
                                  "sheet_ok": False, "sheet_error": tb.strip().splitlines()[-1]}

    template = DASHBOARD_TEMPLATE.read_text(encoding="utf-8")
    data_str = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    return template.replace("__DATA_JSON__", data_str)


# --- A2 step 4: member survey replies (ported from ABC's live_server) ------------
_followup_lock = threading.Lock()
_reply_lock = threading.Lock()


def _members_visible(client) -> bool:
    """Luke 2026-11-02: SHIFT's Members section stays hidden until the member
    survey is switched on (member_survey.enabled true + go_live reached), so
    staff never see an empty section. A non-native client (none here) shows
    whenever it has the block, as at ABC."""
    ms = getattr(client, "member_survey", None) or {}
    if str(ms.get("source", "") or "") != "native":
        return True
    from nudge_tool import member_checkin
    gl = member_checkin.go_live_date(ms)
    return ms.get("enabled") is True and gl is not None and gl <= gym_metrics.eastern_today()


def _reply_send_ready(client) -> bool:
    """Live sending on (same switch as /send) AND Brevo creds on THIS service."""
    if not (_truthy(os.getenv("NUDGE_LIVE_ENABLED")) or getattr(client, "live_enabled", False)):
        return False
    from nudge_tool import survey_dispatch
    try:
        survey_dispatch.brevo_settings(client)
        return True
    except Exception:  # noqa: BLE001
        return False


def followup_now(client_slug: str, req: dict) -> dict:
    """POST /followup, as ABC's (kept for parity; ABC's page no longer shows
    the follow-up card since 10/16, but a reply still writes a row)."""
    with _followup_lock:
        client = load_client(client_slug)
        if not member_views.wants_block(client):
            return {"ok": False, "reason": "no_member_survey",
                    "message": "Membership surveys aren\u2019t set up for this account."}
        ms = client.member_survey or {}
        allowed = [str(a).strip() for a in (ms.get("assignees") or []) if str(a).strip()]
        try:
            member_followups.pull(client)
        except Exception as e:  # noqa: BLE001
            print(f"  (followup: Drive pull failed, appending to local copy: {e})")
        try:
            state = member_followups.record(
                client, req.get("mid", ""), status=req.get("status"),
                assignee=req.get("assignee"), note=req.get("note"),
                by=req.get("by"), allowed_assignees=allowed)
        except member_followups.FollowupError as e:
            return {"ok": False, "reason": "bad_request", "message": str(e)}
        pushed, warning = False, ""
        try:
            pushed = member_followups.push(client)
        except Exception as e:  # noqa: BLE001
            warning = f"Saved here, but the Drive copy didn\u2019t update ({type(e).__name__}). It will retry on the next save."
            print(f"  (followup: Drive push failed: {e})")
        return {"ok": True, "mid": str(req.get("mid", "")).strip(), "state": state,
                "pushed": pushed, "warning": warning}


def _reply_suppressed(client, brevo) -> set:
    """Who a member reply must not go to: Brevo delivery blocks + Shift-sender
    unsubscribes (the shared account also holds ABC's unsubscribes, which do
    not count here; same rule as survey_dispatch) plus Mailchimp
    unsubscribed/cleaned. Raises when either list cannot be read."""
    from nudge_tool import survey_dispatch
    from nudge_tool.mailchimp_client import MailchimpClient
    own = (getattr(getattr(brevo, "s", None), "sender_email", "") or "").strip().lower()
    out = {r["email"] for r in brevo.blocked_contacts() if survey_dispatch._blocks_shift(r, own)}
    out |= {e.strip().lower() for e in MailchimpClient(load_settings(client, require=True)).suppressed_emails()}
    return out


def reply_now(client_slug: str, req: dict) -> dict:
    """POST /reply: ABC's reply_now (one member reply template, sent through
    Brevo from the response page), with SHIFT's pieces swapped in: Brevo
    settings from survey_dispatch (SHIFT's Mailchimp-default config), the
    Shift-branded shell (clients/shift/brevo_shell.html), the member list
    from Beta's Memberships.csv and a FRESH Firestore read of the answers."""
    with _reply_lock:
        client = load_client(client_slug)
        if not member_views.wants_block(client):
            return {"ok": False, "reason": "no_member_survey",
                    "message": "Membership surveys aren\u2019t set up for this account."}
        if not (_truthy(os.getenv("NUDGE_LIVE_ENABLED")) or getattr(client, "live_enabled", False)):
            return {"ok": False, "reason": "not_enabled",
                    "message": "Sending isn\u2019t switched on for this account yet. Use Copy email for now."}
        from nudge_tool import survey_dispatch
        try:
            settings = survey_dispatch.brevo_settings(client)
        except Exception:  # noqa: BLE001
            return {"ok": False, "reason": "no_creds",
                    "message": "Email sending isn\u2019t set up on this server yet. "
                               "Add the Brevo key on the host, then try again."}

        mid = str(req.get("mid", "") or "").strip()
        template_key = str(req.get("template_key", "") or "").strip()
        by = str(req.get("by", "") or "").strip()
        force = bool(req.get("force"))
        subject = req.get("subject")
        body = req.get("body")
        subject = None if subject is None else str(subject)
        body = None if body is None else str(body)
        if (subject is not None or body is not None) and \
                not ((subject or "").strip() and (body or "").strip()):
            return {"ok": False, "reason": "bad_request",
                    "message": "The subject and the email text cannot be blank."}
        if not mid:
            return {"ok": False, "reason": "bad_request", "message": "mid is required."}
        if template_key not in member_reply.reply_templates():
            return {"ok": False, "reason": "bad_request",
                    "message": f"{template_key or '(blank)'} is not a member reply template."}

        from requests.exceptions import RequestException

        from nudge_tool.brevo_client import BrevoClient
        from nudge_tool.config import ESPError
        from nudge_tool.mailchimp_client import MailchimpError

        _ensure_beta_data(client)
        members = member_responses_native.member_index(client)
        m = members.get(mid)
        if m is None:
            return {"ok": False, "reason": "no_member",
                    "message": "This response isn\u2019t matched to a member, so there is nobody to email."}
        email = (m.get("email") or "").strip().lower()
        if not email:
            return {"ok": False, "reason": "no_email",
                    "message": "This member has no email address on file."}
        try:
            rr = member_responses_native.read(client, fresh=True)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": "sheet",
                    "message": f"Couldn\u2019t read the survey answers ({type(e).__name__}), so nothing was sent. Try again in a moment."}
        rid = str(req.get("rid", "") or "").strip()
        mine = sorted((r for r in rr.responses if r.mid == mid),
                      key=lambda r: (r.answered_at, r.respondent_id))
        resp = next((r for r in mine if r.rid == rid), None) if rid else (mine[-1] if mine else None)
        if resp is None:
            return {"ok": False, "reason": "no_response",
                    "message": "That response is no longer on file. Refresh the page."}

        esp_client = BrevoClient(settings, client)
        try:
            unsub = _reply_suppressed(client, esp_client)
        except (RuntimeError, ESPError, MailchimpError, RequestException):
            return {"ok": False, "reason": "no_unsub",
                    "message": "Couldn\u2019t reach the email service to check unsubscribes, "
                               "so nothing was sent. Try again in a moment."}
        if email in unsub:
            return {"ok": False, "reason": "suppressed",
                    "message": "This member has unsubscribed or their address bounced, so the email service won\u2019t deliver to them."}

        prior = member_reply.replied_since(
            member_reply.replies_by_mid(outreach.load_rows(client)).get(mid, []),
            resp.answered_at)
        if prior and not force:
            last = prior[-1]
            return {"ok": False, "reason": "already_replied", "replies": prior,
                    "message": f"A reply already went out on {last['date']} "
                               f"({member_reply.title_of(last['template_key'])}). "
                               f"Click Send again to send another anyway."}

        today = date.today().isoformat()
        first_name = m.get("first_name") or ""
        try:
            orig_subject, orig_body = member_reply.rendered_for(client, template_key, first_name)
            row = member_reply.send(esp_client, client, mid=mid, email=email,
                                    first_name=first_name,
                                    template_key=template_key, today=today,
                                    subject=subject, body=body)
        except member_reply.ReplyError as e:
            return {"ok": False, "reason": "bad_request", "message": str(e)}
        except (ESPError, RequestException) as e:
            return {"ok": False, "reason": "send_failed",
                    "message": f"The email service refused the send ({e}). Nothing was logged."}
        sent_subject = member_reply.fill_name(subject, first_name).strip() if subject is not None else orig_subject
        sent_body = member_reply.fill_name(body, first_name).strip() if body is not None else orig_body
        outreach.append(client, [row])
        pushed, warning = False, ""
        try:
            _push_outreach_log(client)
            pushed = bool(os.getenv("OUTREACH_DRIVE_ID"))
        except Exception as e:  # noqa: BLE001
            warning = f"Sent, but the Drive copy of the log didn\u2019t update ({type(e).__name__})."
            print(f"  (reply: outreach push failed: {e})")

        state = None
        thr = member_survey.load_config(client).positive_threshold
        if resp.is_detractor(thr):
            try:
                try:
                    member_followups.pull(client)
                except Exception as e:  # noqa: BLE001
                    print(f"  (reply: followup pull failed, appending to local copy: {e})")
                cur = member_followups.latest_by_mid(client).get(mid)
                status = "waiting" if (cur is None or cur["status"] in ("open", "in_progress")) else None
                state = member_followups.record(
                    client, mid, status=status,
                    note=f"Emailed the reply: {member_reply.title_of(template_key)}",
                    by=by or "dashboard")
                try:
                    member_followups.push(client)
                except Exception as e:  # noqa: BLE001
                    print(f"  (reply: followup push failed: {e})")
            except Exception as e:  # noqa: BLE001
                print(f"  (reply: follow-up note not recorded: {e})")

        edit_saved, edit_changed = False, False
        try:
            try:
                member_reply_edits.pull(client)
            except Exception as e:  # noqa: BLE001
                print(f"  (reply: edits pull failed, appending to local copy: {e})")
            erow = member_reply_edits.record(
                client, mid=mid, template_key=template_key, by=by or "dashboard",
                original_subject=orig_subject, edited_subject=sent_subject,
                original_body=orig_body, edited_body=sent_body)
            edit_saved, edit_changed = True, erow["changed"] == "1"
            try:
                member_reply_edits.push(client)
            except Exception as e:  # noqa: BLE001
                print(f"  (reply: edits push failed: {e})")
                warning = warning or "Sent, but the Drive copy of the edit record didn\u2019t update."
        except Exception as e:  # noqa: BLE001
            print(f"  (reply: edit pair NOT saved: {e})")
            warning = warning or "Sent, but the edit record could not be saved."

        reply = {"date": today, "template_key": template_key,
                 "trigger": member_reply.REPLY_TRIGGER, "auto": False,
                 "edited": edit_changed}
        return {"ok": True, "mid": mid, "email": email, "reply": reply,
                "title": member_reply.title_of(template_key), "state": state,
                "pushed": pushed, "warning": warning,
                "edit_saved": edit_saved, "edit_changed": edit_changed}


def _inject_refresh(html: str, seconds: int) -> str:
    tag = f'<meta http-equiv="refresh" content="{seconds}">'
    return html.replace("<head>", "<head>\n  " + tag, 1)


def _error_page(msg: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Live dashboard — error</title>"
        "<style>body{font-family:system-ui,Arial,sans-serif;max-width:640px;"
        "margin:60px auto;padding:0 20px;color:#1a2b34}"
        "pre{background:#f4f6f8;padding:14px;border-radius:8px;overflow:auto;"
        "font-size:13px;color:#7a1f1f}</style></head><body>"
        "<h2>Live dashboard hit a snag</h2>"
        "<p>The page tried to read fresh data and the engine raised an error. "
        "The underlying cron+static demo is unaffected. Detail:</p>"
        f"<pre>{msg}</pre>"
        "<p>Refresh to retry.</p></body></html>"
    )


# --- survey Q1 tap-through (/s) ---------------------------------------------
# The embed-variant survey email (2x2 test, 2026-07-28) renders Q1 as five
# rating buttons. Each button hits GET /s?q=<1-5>&e=<recipient>&t=<variant tag>
# UNAUTHENTICATED (recipients don't have the dashboard's Basic Auth), which
# (a) logs the tap to the response sheet's Taps tab in a background thread, so
# tap-and-bail still captures the Q1 answer, and (b) 302-redirects into the
# Google Form with Q1 + Email prefilled so a Submit still lands the full
# response. A logging failure never blocks the redirect.
_TAP_RW_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
_TAP_TAG_OK = __import__("re").compile(r"^[A-Za-z0-9_.-]{0,64}$")


def _tap_cfg(slug: str) -> dict:
    """{'sheet_id','tap_range','q1_entry','email_entry','form_url'} or {} if
    the client has no q1_tap config."""
    try:
        client = load_client(slug)
    except Exception:
        return {}
    tap = dict((client.survey or {}).get("q1_tap") or {})
    if not tap:
        return {}
    tap.setdefault("sheet_id", (client.survey or {}).get("gsheet_id", ""))
    tap["form_url"] = (client.links or {}).get("survey_link", "")
    return tap


def _log_tap(tap: dict, email: str, q: str, tag: str) -> None:
    """Append one tap row. Runs on a daemon thread; errors print, never raise."""
    try:
        from googleapiclient.discovery import build
        from nudge_tool import drive_io
        creds = drive_io._creds(_TAP_RW_SCOPE)
        svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        row = [datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), email, q, tag]
        svc.spreadsheets().values().append(
            spreadsheetId=tap["sheet_id"], range=tap.get("tap_range", "Taps!A1"),
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute(num_retries=3)
        print(f"  /s tap logged: {email} q1={q} tag={tag}")
    except Exception as exc:
        print(f"  /s tap LOG FAILED (redirect already served): {exc}")


def _tap_redirect(tap: dict, email: str, q: str) -> str:
    """The prefilled-form URL to bounce the tapper into."""
    from urllib.parse import quote
    base = tap.get("form_url", "")
    if not base:
        return ""
    parts = [f"entry.{tap['q1_entry']}={quote(q, safe='')}"] if q else []
    if email and tap.get("email_entry"):
        parts.append(f"entry.{tap['email_entry']}={quote(email, safe='')}")
    if not parts:
        return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}usp=pp_url&" + "&".join(parts)


# --- native survey island (ROADMAP Block 11, 2026-09-21) --------------------
# When client.json survey.native.enabled is true AND this service has the
# Firestore env (FIRESTORE_PROJECT_ID + key file + NATIVE_SURVEY_PUBLIC_BASE_URL),
# a survey click mints the Firestore record Chris's page needs and lands there
# instead of the Google Form. Anything missing or failing = the Form, exactly
# as before. The tap is still logged to the Taps tab first (when q is present).
# `q` is optional so the plain "Take the quick survey" link can go through /s.
_refresh_for_s = _ensure_beta_data      # this tree's Drive-pull helper (name differs per gym)
_climber_index: dict = {}               # slug -> (monotonic stamp, {email: climber_id})
_CLIMBER_INDEX_TTL = int(os.getenv("S_CLIMBER_INDEX_TTL", "600") or "600")
_climber_index_lock = threading.Lock()


def _climber_id_for(slug: str, email: str) -> str:
    """email -> climber id from this client's dataset, cached per slug so one
    click pays the ingest and the next ten minutes of clicks do not. "" when
    the email is not a known climber (then the Form gets the click)."""
    key = (email or "").strip().lower()
    if not key:
        return ""
    with _climber_index_lock:
        ent = _climber_index.get(slug)
        now = time.monotonic()
        if ent is None or (now - ent[0]) > _CLIMBER_INDEX_TTL:
            client = load_client(slug)
            try:
                _refresh_for_s(client)
            except Exception as exc:  # noqa: BLE001 (stale local data still answers)
                print(f"  /s: data refresh failed ({exc}); using local files")
            idx: dict = {}
            for c in ingest.load(client).climbers.values():
                e = (c.email or "").strip().lower()
                if e and e not in idx:
                    idx[e] = c.climber_id
            ent = (now, idx)
            _climber_index[slug] = ent
    return ent[1].get(key, "")


def _native_survey_url(slug: str, email: str, q: str, tag: str) -> str:
    """Chris's page URL with a fresh token, or "" (= use the Form). Never raises."""
    try:
        from nudge_tool import native_survey
        client = load_client(slug)
        if not email or not native_survey.mint_enabled(client):
            return ""
        cid = _climber_id_for(slug, email)
        if not cid:
            print("  /s: email is not a known climber; Form fallback")
            return ""
        return native_survey.mint(client, email, cid, q1=int(q) if q else None, tag=tag)
    except Exception as exc:  # noqa: BLE001
        print(f"  /s: native survey FAILED ({type(exc).__name__}: {exc}); Form fallback")
        return ""


# Block 26 (2026-10-21): a click into a survey that already has saved answers
# gets this page, never a token (mint returns native_survey.ALREADY_STARTED).
_ALREADY_STARTED = "already_started"
_THANKS_PAGE = (
    b'<!doctype html><html lang="en"><head><meta charset="utf-8">'
    b'<meta name="viewport" content="width=device-width, initial-scale=1">'
    b'<title>Thanks</title></head>'
    b'<body style="font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;'
    b'max-width:32rem;margin:15vh auto;padding:0 1rem;text-align:center;color:#222">'
    b'<h1 style="font-size:1.5rem">Thanks, we already have your answers.</h1>'
    b'<p>You can close this page.</p></body></html>')


def _s_redirect_url(slug: str, tap: dict, email: str, q: str, tag: str) -> str:
    """Where a survey click lands: the native page when the island is on and
    the mint succeeds, else the prefilled Google Form ("" = no survey at all).
    _ALREADY_STARTED when that climber's survey already has saved answers."""
    url = _native_survey_url(slug, email, q, tag)
    if url:
        return url
    return _tap_redirect(tap, email, q) if tap else ""


# Block 18 (2026-10-02): Chris's survey page served from this process. The base
# class is the real mount only when this service has the Firestore env;
# otherwise a stdlib stub that answers 503 on the page paths. See
# nudge_tool/survey_page/__init__.py.
from nudge_tool import survey_page  # noqa: E402


# A survey capability must never reach the log. On the page paths the WHOLE
# query goes (covers tok%65n=, "token= <x>", any spelling); elsewhere token=.
_RE = __import__("re")
_PAGE_QUERY_RE = _RE.compile(r'(/(?:member-)?survey[^\s?"]*\?)[^"]*')  # A2: /member-survey too
_TOKEN_RE = _RE.compile(r"(token=)[^\s&\"']+")


def _redact(line: str) -> str:
    return _TOKEN_RE.sub(r"\1<redacted>", _PAGE_QUERY_RE.sub(r"\1<redacted>", line))


# --- Block 25 A1 (2026-10-23): routes the Brevo survey emails link to --------
# /survey-tap?token=   an embed email's rating button. The token was minted at
#                      SEND time (native_survey.issue_send_tokens) and carries
#                      the 1-5 candidate. Logs the tap to the Taps tab only when
#                      the token checks out against Firestore (so, unlike /s,
#                      nobody can forge rows), then 302s to /survey?token=.
# /survey-preference   the Shift-only survey opt-out page (Chris's preference
#                      page, adapted; nudge_tool/survey_optout.py). GET shows
#                      it, POST .../opt-out saves (Bearer token), POST
#                      .../one-click is the List-Unsubscribe one-click target.
#                      A GET on .../one-click (a mail app that opens the header
#                      link in a browser) shows the same page.
# All unauthenticated (recipients have no dashboard login); the paths start with
# /survey, so the log redaction below already keeps their tokens out of the log.
_SURVEY_TOKEN_OK = _RE.compile(r"[a-f0-9]{64}\.[A-Za-z0-9_-]{43}")
_OPTOUT_STATIC = {"/survey-preference.js": ("survey_optout_page.js",
                                            "text/javascript; charset=utf-8"),
                  "/survey-preference.css": ("survey_optout_page.css",
                                             "text/css; charset=utf-8")}
_OPTOUT_MESSAGES = {
    "none_recorded": ("Click the button to stop survey emails from Shift. Other "
                      "emails from Shift (news, offers) are not affected.", ""),
    "opted_out": ("You’re already opted out of survey emails from Shift.", "hidden"),
    None: ("This link is invalid or has expired.", "hidden"),
    "error": ("We couldn’t load your choice right now. Please try again later.", "hidden"),
}


def _log_send_tap(slug: str, token: str) -> None:
    """Background: check the token, then append its Taps row. Never raises."""
    try:
        from nudge_tool import native_survey
        info = native_survey.tap_info(load_client(slug), token)
        tap = _tap_cfg(slug)
        if info and tap and info.get("email") and info.get("candidate") in (1, 2, 3, 4, 5):
            _log_tap(tap, info["email"], str(info["candidate"]), info.get("tag", ""))
        elif not info:
            print("  /survey-tap: token did not check out; no tap logged")
    except Exception as exc:  # noqa: BLE001
        print(f"  /survey-tap: tap log FAILED ({type(exc).__name__})")


# --- member survey tap buttons (2026-11-06, ROADMAP "Tap buttons in every ------
# survey email" piece 1; Luke picked "save on page load") -----------------------
# /member-tap?token=&n=   one of the eleven 0-10 buttons in the member survey
#                         email (survey_mail.MEMBER_TAP_PATH). Serves the tiny
#                         page below; its script reads the member page's own
#                         /member-survey/state, saves n as core_nps through the
#                         member page's own save route (same token, same Origin
#                         + revision checks as Chris's page), then replaces
#                         itself with /member-survey?token=, where the number
#                         shows as picked. The GET itself writes NOTHING, so a
#                         link checker that only fetches the URL saves no score.
#                         A finished survey is never changed; a different
#                         number tapped later replaces an unfinished one. Any
#                         failure still opens the survey (just without the
#                         number). The access log redacts token= as everywhere.
_MEMBER_TAP_QUESTION = "core_nps"
_MEMBER_TAP_SCORES = frozenset(str(n) for n in range(11))
_MEMBER_TAP_HTML = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<meta name="referrer" content="no-referrer">'
    '<meta name="robots" content="noindex">'
    '<title>Opening your survey</title></head>'
    '<body style="font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;'
    'max-width:32rem;margin:15vh auto;padding:0 1rem;text-align:center;color:#222">'
    '<p>Opening your survey&hellip;</p>'
    '<noscript><p><a href="/member-survey?token=__TOKEN__">Open the survey</a></p></noscript>'
    '<script src="/member-tap.js"></script></body></html>')
_MEMBER_TAP_JS = ("""'use strict';
(async()=>{
const u=new URL(location.href),token=u.searchParams.get('token')||'',n=Number(u.searchParams.get('n'));
const QID='""" + _MEMBER_TAP_QUESTION + """';
const go=()=>location.replace('/member-survey?token='+encodeURIComponent(token));
const opt=()=>({cache:'no-store',...(AbortSignal.timeout?{signal:AbortSignal.timeout(8000)}:{})});
function rid(){if(crypto.randomUUID)return crypto.randomUUID();const b=new Uint8Array(16);crypto.getRandomValues(b);return Array.from(b,x=>x.toString(16).padStart(2,'0')).join('')}
try{
const r=await fetch('/member-survey/state?token='+encodeURIComponent(token),opt());
if(!r.ok)return go();
const s=await r.json(),q=(s.questions||[]).find(x=>x.id===QID),cur=(s.answers||{})[QID];
if(s.state==='completed'||s.read_only||!q||q.kind!=='rating'||!(s.applicability||{})[QID]
||!Number.isInteger(n)||n<q.minimum||n>q.maximum||(cur&&cur.status==='answered'&&cur.value===n))return go();
await fetch('/member-survey/answer',{...opt(),method:'POST',
headers:{'Content-Type':'application/json',Authorization:'Bearer '+token},
body:JSON.stringify({request_id:rid(),command:{kind:'answer',expected_revision:s.revision,
answers:{[QID]:{status:'answered',value:n}}}})});
}catch(e){}
go();
})();
""").encode("utf-8")
_MEMBER_TAP_CSP = ("default-src 'none'; script-src 'self'; connect-src 'self'; "
                   "style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; "
                   "frame-ancestors 'none'")


class Handler(survey_page.handler_base()):
    timeout = 30  # seconds a client may stall a connection (fresh-eyes 10/2: slowloris on the page POST)

    def log_message(self, fmt, *args):  # quieter console; a survey token never reaches the log (Block 18)
        print("  " + _redact(fmt % args))

    def _authed(self) -> bool:
        """True if Basic Auth is off, or the request carries the right creds
        (the shared login or a per-person LIVE_PASS_<NAME> one)."""
        if not AUTH_ON:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                if BASIC_USER and BASIC_PASS and user == BASIC_USER and pw == BASIC_PASS:
                    return True
                named = NAMED_LOGINS.get(user.strip().lower())
                if named and hmac.compare_digest(pw.encode(), named.encode()):
                    return True
            except Exception:
                pass
        return False

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
        except ValueError:  # e.g. an absolute-form target "http://[x/..." (fresh-eyes 10/2)
            self._send(400, "text/plain; charset=utf-8", b"bad request")
            return
        params = parse_qs(parsed.query)

        if parsed.path == "/healthz":  # unauthenticated so uptime checks work
            self._send(200, "text/plain; charset=utf-8", b"ok")
            return

        if parsed.path == "/s":  # unauthenticated: survey click-through (Q1 tap or plain link)
            slug = params.get("c", [self.server.default_client])[0]
            tap = _tap_cfg(slug)
            q = (params.get("q", [""])[0] or "").strip()
            # Mailchimp's *|URL:EMAIL|* merge tag URL-encodes; a raw *|EMAIL|*
            # would arrive with '+' decoded to ' '. Emails never contain
            # spaces, so restoring '+' here is safe either way.
            email = (params.get("e", [""])[0] or "").strip().replace(" ", "+")[:254]
            tag = (params.get("t", [""])[0] or "").strip()
            if q not in {"1", "2", "3", "4", "5"}:
                q = ""
            if not _TAP_TAG_OK.match(tag):
                tag = ""
            if tap and q and email:
                threading.Thread(target=_log_tap, args=(tap, email, q, tag),
                                 daemon=True).start()
            url = _s_redirect_url(slug, tap, email, q, tag)
            if url == _ALREADY_STARTED:  # Block 26: saved answers never reopen from /s
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(_THANKS_PAGE)))
                self.end_headers()
                self.wfile.write(_THANKS_PAGE)
            elif url:
                self.send_response(302)
                self.send_header("Location", url)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._send(404, "text/plain; charset=utf-8", b"unknown survey")
            return

        if parsed.path == "/survey-tap":  # Block 25 A1: Brevo embed button, unauthenticated
            token = (params.get("token", [""])[0] or "").strip()
            if not _SURVEY_TOKEN_OK.fullmatch(token):
                self._send(404, "text/plain; charset=utf-8", b"unknown survey")
                return
            threading.Thread(target=_log_send_tap,
                             args=(self.server.default_client, token), daemon=True).start()
            self.send_response(302)
            self.send_header("Location", "/survey?" + urlencode({"token": token}))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if parsed.path in ("/member-tap", "/member-tap.js"):  # member email 0-10 button, unauthenticated
            self._member_tap(parsed.path, params)
            return

        if parsed.path in ("/survey-preference", "/survey-preference/one-click"):
            self._optout_page((params.get("token", [""])[0] or "").strip())  # Block 25 A1
            return
        if parsed.path in _OPTOUT_STATIC:
            from nudge_tool import survey_optout
            name, ctype = _OPTOUT_STATIC[parsed.path]
            self._send(200, ctype, survey_optout.page_asset(name))
            return

        if survey_page.is_page_path(parsed.path):  # Block 18: Chris's survey page, unauthenticated like /s
            self.page_get()
            return
        if parsed.path == "/member-qr":  # QR sign (2026-11-05), unauthenticated
            self._member_qr_get()
            return

        if not self._authed():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="SHIFT live"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if parsed.path == "/favicon.ico":
            self._send(204, "text/plain", b"")
            return

        slug = params.get("client", [self.server.default_client])[0]
        try:
            asof = _resolve_asof(params, slug)
            html = render(slug, asof)
            refresh = int(params["refresh"][0]) if params.get("refresh") else DEFAULT_REFRESH
            if refresh > 0:
                html = _inject_refresh(html, refresh)
            self._send(200, "text/html; charset=utf-8", html.encode("utf-8"))
        except Exception:
            tb = traceback.format_exc()
            print("  ERROR rendering:\n" + tb)
            self._send(500, "text/html; charset=utf-8",
                       _error_page(tb).encode("utf-8"))

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
        except ValueError:  # same malformed-target case as do_GET
            self._send(400, "text/plain; charset=utf-8", b"bad request")
            return
        if survey_page.is_page_path(parsed.path):  # Block 18: the page's own save route (his checks apply)
            self.page_post()
            return
        if parsed.path in ("/survey-preference/opt-out", "/survey-preference/one-click"):
            self._optout_post(parsed)  # Block 25 A1: unauthenticated, the token is the key
            return
        if parsed.path == "/member-qr":  # QR sign (2026-11-05), unauthenticated
            self._member_qr_post(parsed)
            return
        if not self._authed():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="SHIFT live"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if parsed.path not in ("/send", "/followup", "/reply"):  # A2: + member reply routes
            self._send(404, "application/json", b'{"ok":false,"message":"Not found."}')
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b""
            req = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(req, dict):
                req = {}
        except Exception:
            req = {}
        slug = self.server.default_client
        if parsed.path in ("/followup", "/reply"):  # A2 step 4 (ABC's handlers, ported)
            fn = followup_now if parsed.path == "/followup" else reply_now
            try:
                result = fn(slug, req)
            except Exception:
                tb = traceback.format_exc()
                print(f"  ERROR in {parsed.path}:\n" + tb)
                result = {"ok": False,
                          "message": ("Something went wrong while sending the reply; it may not have gone out. "
                                      "Refresh the page: a sent reply shows under Replies sent.")
                          if parsed.path == "/reply" else
                          "Something went wrong saving the follow-up. Refresh and check whether it stuck."}
            body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
            return
        try:
            result = send_now(slug, req)
        except Exception:
            tb = traceback.format_exc()
            print("  ERROR in /send:\n" + tb)
            result = {"ok": False,
                      "message": "Something went wrong while sending; nothing may have gone out. "
                                 "Check the report email."}
        body = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self._send(200, "application/json; charset=utf-8", body)

    def _member_tap(self, path: str, params: dict) -> None:
        """GET /member-tap (the page) or /member-tap.js (its script). See the
        _MEMBER_TAP_* block above. A bad token = 404; a bad n = straight to
        the survey (no number). Writes nothing itself."""
        if path == "/member-tap.js":
            body, ctype = _MEMBER_TAP_JS, "text/javascript; charset=utf-8"
            code = 200
        else:
            raw_token = params.get("token", [""])[0] or ""
            raw_n = params.get("n", [""])[0] or ""
            token, n = raw_token.strip(), raw_n.strip()
            if not _SURVEY_TOKEN_OK.fullmatch(token):
                self._send(404, "text/plain; charset=utf-8", b"unknown survey")
                return
            if (token, n) != (raw_token, raw_n) and n in _MEMBER_TAP_SCORES:
                # a mangled link (stray space): the page script reads the RAW
                # address, so bounce to the clean one first (fresh-eyes 11/06)
                self.send_response(302)
                self.send_header("Location", "/member-tap?" + urlencode({"token": token, "n": n}))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if n not in _MEMBER_TAP_SCORES:
                self.send_response(302)
                self.send_header("Location", "/member-survey?" + urlencode({"token": token}))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            print(f"  /member-tap: n={n}")  # proof of a real tap in the Render log (no token)
            # token passed the shape check (hex . urlsafe), so it is safe in HTML as is
            body, ctype = _MEMBER_TAP_HTML.replace("__TOKEN__", token).encode("utf-8"), "text/html; charset=utf-8"
            code = 200
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Robots-Tag", "noindex")
        self.send_header("Content-Security-Policy", _MEMBER_TAP_CSP)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _optout_page(self, token: str) -> None:
        """GET /survey-preference: Chris's page with the state for this link.
        Read-only. The token stays in the page's own JS (it strips it from the
        address bar and sends it as a Bearer header on the click)."""
        from nudge_tool import survey_optout
        state: str | None = None
        if _SURVEY_TOKEN_OK.fullmatch(token or ""):
            try:
                state = survey_optout.status(load_client(self.server.default_client), token)
            except Exception as exc:  # noqa: BLE001
                print(f"  /survey-preference: status FAILED ({type(exc).__name__})")
                state = "error"
        msg, button = _OPTOUT_MESSAGES[state]
        from html import escape
        body = (survey_optout.page_asset("survey_optout_page.html").decode("utf-8")
                .replace("__MESSAGE__", escape(msg))
                .replace("__BUTTON_STATE__", button)).encode("utf-8")
        self.send_response(503 if state == "error" else 200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Robots-Tag", "noindex")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _optout_post(self, parsed) -> None:
        """POST .../opt-out (page button: Bearer token + {"request_id"}) or
        .../one-click?token= (RFC 8058, the mail app's own Unsubscribe).
        200 saved, 403 bad link, 400 malformed body, 503 Firestore trouble."""
        from nudge_tool import survey_optout
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = -1
        if not 0 <= length <= 4096:
            self._send(400, "application/json", b'{"error":"bad_request"}')
            return
        raw = self.rfile.read(length) if length else b""
        if parsed.path.endswith("/one-click"):
            token = (parse_qs(parsed.query).get("token", [""])[0] or "").strip()
            request_id, source = "one_click", "list_unsubscribe"
        else:
            auth = self.headers.get("Authorization", "")
            token = auth[7:].strip() if auth.startswith("Bearer ") else ""
            try:
                req = json.loads(raw.decode("utf-8")) if raw else {}
                request_id = str((req or {}).get("request_id") or "") if isinstance(req, dict) else ""
            except Exception:  # noqa: BLE001
                request_id = ""
            source = "preference_page"
        if not _SURVEY_TOKEN_OK.fullmatch(token or ""):
            self._send(403, "application/json", b'{"error":"invalid"}')
            return
        try:
            res = survey_optout.opt_out(load_client(self.server.default_client), token,
                                        request_id, source=source)
        except Exception as exc:  # noqa: BLE001
            print(f"  /survey-preference: opt-out FAILED ({type(exc).__name__})")
            self._send(503, "application/json", b'{"error":"unavailable"}')
            return
        if res == "opted_out":
            print(f"  /survey-preference: opt-out recorded ({source})")
            self._send(200, "application/json", b'{"preference":"opted_out"}')
        elif not request_id:
            self._send(400, "application/json", b'{"error":"bad_request"}')
        else:
            self._send(403, "application/json", b'{"error":"invalid"}')

    # --- QR sign member survey (2026-11-05; nudge_tool/member_qr.py) -------
    # GET  /member-qr   the email form (or "not open" when member_qr is off)
    # POST /member-qr   email (+ pick when an inbox has several members) ->
    #                   303 to /member-survey?token=, or a message page.
    # Unauthenticated (members have no dashboard login). The log line carries
    # the path only; the email never reaches the log, the token only in the
    # Location header.
    def _qr_html(self, body: bytes, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # same-origin, NOT no-referrer: under no-referrer Chrome/Edge post the
        # form with "Origin: null" and the Origin check below refused every
        # member (fresh-eyes 11/05). No token is ever on these pages.
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _member_qr_get(self) -> None:
        from nudge_tool import member_qr
        try:
            on = member_qr.is_on(member_qr.load_cfg(load_client(self.server.default_client)))
        except Exception as exc:  # noqa: BLE001
            print(f"  /member-qr: config FAILED ({type(exc).__name__})")
            on = False
        self._qr_html(member_qr.page_form() if on else member_qr.page_message("off"))

    def _client_ip(self) -> str:
        """The LAST X-Forwarded-For hop (the one Render's proxy adds; earlier
        hops are whatever the sender typed), else the socket peer. Not proof
        of anything: the gym-wide miss cap is the real enumeration guard."""
        hops = [h.strip() for h in (self.headers.get("X-Forwarded-For") or "").split(",") if h.strip()]
        return hops[-1] if hops else (self.client_address[0] if self.client_address else "")

    def _member_qr_post(self, parsed) -> None:
        from nudge_tool import member_qr
        origin = self.headers.get("Origin")
        if origin is not None and urlparse(origin).netloc != (self.headers.get("Host") or ""):
            self._qr_html(member_qr.page_message("error"), 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = -1
        if parsed.query or not 0 < length <= 2048:
            self._qr_html(member_qr.page_message("error"), 400)
            return
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        email = (form.get("email", [""])[0] or "").strip().lower()[:254]
        pick = (form.get("pick", [""])[0] or "").strip()[:32]
        if not member_qr.LIMITER.allow(self._client_ip(), email):
            print("  /member-qr: rate limit")
            self._qr_html(member_qr.page_message("busy"), 429)
            return
        try:
            client = load_client(self.server.default_client)
            if not member_qr.is_on(member_qr.load_cfg(client)):
                self._qr_html(member_qr.page_message("off"))
                return
            if not email:
                self._qr_html(member_qr.page_form("Type the email on your membership."))
                return
            try:
                _ensure_beta_data(client)
            except Exception as exc:  # noqa: BLE001 (local files still answer)
                print(f"  /member-qr: data refresh failed ({type(exc).__name__}); using local files")
            if not client.outreach_log.exists():
                raise RuntimeError("outreach log missing (cannot check the 90 days)")
            out = member_qr.decide(client, email, pick, log=outreach.load(client))
        except Exception as exc:  # noqa: BLE001
            print(f"  /member-qr: FAILED ({type(exc).__name__}: {exc})")
            self._qr_html(member_qr.page_message("error"), 503)
            return
        if out.kind == "not_member" or out.note == "bad pick":
            # a miss: counts toward the IP + gym-wide caps; members never do
            if member_qr.LIMITER.gym_full():
                print("  /member-qr: gym-wide miss cap")
                self._qr_html(member_qr.page_message("busy"), 429)
                return
            member_qr.LIMITER.miss(self._client_ip())
        print(f"  /member-qr: {out.kind}" + (f" ({out.note})" if out.note else ""))
        if out.kind == "survey":
            self.send_response(303)
            self.send_header("Location", "/member-survey?" + urlencode({"token": out.token}))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif out.kind == "pick":
            self._qr_html(member_qr.page_pick(email, out.choices))
        else:
            self._qr_html(member_qr.page_message(out.kind))

    def _send(self, code: int, ctype: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)


def main() -> None:
    # On a host (Render/Fly/Railway) PORT is injected and we must bind 0.0.0.0;
    # locally these env vars are unset so the safe 127.0.0.1:8000 default holds.
    env_port = os.getenv("PORT")
    ap = argparse.ArgumentParser(description="Live (read-on-load) SHIFT dashboard server.")
    ap.add_argument("--port", type=int, default=int(env_port) if env_port else 8000)
    ap.add_argument("--host", default="0.0.0.0" if env_port else "127.0.0.1")
    ap.add_argument("--client", default=os.getenv("LIVE_CLIENT", DEFAULT_CLIENT),
                    help=f"client slug (default {DEFAULT_CLIENT}; known: "
                         f"{', '.join(config.list_clients())})")
    args = ap.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.default_client = args.client
    threading.Thread(target=_activity_refresher, args=(args.client,),
                     daemon=True).start()
    # the All Gyms sheet's email results for the Insights email slides
    # (Block 15 session 3); a no-op thread for a demo client
    threading.Thread(target=email_results.refresher, args=(args.client,),
                     daemon=True).start()
    url = f"http://{args.host}:{args.port}/"
    print("Live dashboard server (additive, read-only)")
    print(f"  client : {args.client}")
    print(f"  open   : {url}")
    print(f"  auth   : {'Basic Auth ON' if AUTH_ON else 'OPEN (no LIVE_USER/LIVE_PASS set)'}"
          + (f" | named logins: {', '.join(sorted(NAMED_LOGINS))}" if NAMED_LOGINS else ""))
    print(f"  as-of  : today + {DEFAULT_DAYS_AHEAD}d by default (override ?asof= or ?days=)")
    print("  the cron+static demo is untouched and still your backup.")
    print("  Ctrl+C to stop.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
