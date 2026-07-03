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
import json
import os
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from nudge_tool import config, engine, ingest, livesend, outreach, survey
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
    _last_pull = now
    print(f"  pulled from Drive: {', '.join(pulled)}")

# Optional login gate (protects the real PII on the hosted /live page). When
# LIVE_USER + LIVE_PASS env vars are set, every request must pass HTTP Basic
# Auth. Unset (the local-dev default) = no gate, exactly as before. This is the
# app-level fallback; a fronting Cloudflare Access policy can gate it instead.
BASIC_USER = os.getenv("LIVE_USER")
BASIC_PASS = os.getenv("LIVE_PASS")

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
_activity_lock = threading.Lock()


def _refresh_activity_once(client_slug: str) -> None:
    global _activity_map
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
    fetched: dict = {}
    for e in emails:
        try:
            fetched[e] = mc.member_activity(e)
        except (MailchimpError, RequestException, RuntimeError):
            prev = (_activity_map or {}).get(e)
            if prev is not None:
                fetched[e] = prev  # keep the last good read for this contact
    with _activity_lock:
        _activity_map = fetched


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
        sent: list = []
        failed: list = []
        for t in targets:
            try:
                mc.tag_member(t.email, t.first_name, t.tag)
                sent.append(t)
            except (MailchimpError, RequestException) as e:
                failed.append({"name": t.name, "email": t.email, "error": str(e)})
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

    suppressed_out: list = []
    queue = engine.build_queue(ds, client, asof, log=log,
                               unsubscribed=set(), suppressed_out=suppressed_out,
                               dedup_email=True, own_email_guard=True)
    generated_at = datetime.now().isoformat(timespec="seconds")
    with _activity_lock:
        activity = _activity_map
    payload = engine.build_payload(ds, queue, client, asof, "dry-run",
                                   generated_at, survey_result, suppressed_out,
                                   sent_rows=outreach.load_rows(client),
                                   activity_by_email=activity)

    payload["send_enabled"] = _send_enabled(client)

    template = DASHBOARD_TEMPLATE.read_text(encoding="utf-8")
    data_str = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    return template.replace("__DATA_JSON__", data_str)


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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter console
        print("  " + (fmt % args))

    def _authed(self) -> bool:
        """True if Basic Auth is off, or the request carries the right creds."""
        if not (BASIC_USER and BASIC_PASS):
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                if user == BASIC_USER and pw == BASIC_PASS:
                    return True
            except Exception:
                pass
        return False

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == "/healthz":  # unauthenticated so uptime checks work
            self._send(200, "text/plain; charset=utf-8", b"ok")
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
        parsed = urlparse(self.path)
        if not self._authed():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="SHIFT live"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if parsed.path != "/send":
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
    url = f"http://{args.host}:{args.port}/"
    print("Live dashboard server (additive, read-only)")
    print(f"  client : {args.client}")
    print(f"  open   : {url}")
    print(f"  auth   : {'Basic Auth ON' if (BASIC_USER and BASIC_PASS) else 'OPEN (no LIVE_USER/LIVE_PASS set)'}")
    print(f"  as-of  : today + {DEFAULT_DAYS_AHEAD}d by default (override ?asof= or ?days=)")
    print("  the cron+static demo is untouched and still your backup.")
    print("  Ctrl+C to stop.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
