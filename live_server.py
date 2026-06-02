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
import traceback
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from nudge_tool import config, engine, ingest, outreach, survey
from nudge_tool.config import DASHBOARD_TEMPLATE, load_client

DEFAULT_CLIENT = "shift_demo_live"
DEFAULT_DAYS_AHEAD = 6  # as-of = today + 6 so a same-day submission routes to its nudge

# Optional login gate (protects the real PII on the hosted /live page). When
# LIVE_USER + LIVE_PASS env vars are set, every request must pass HTTP Basic
# Auth. Unset (the local-dev default) = no gate, exactly as before. This is the
# app-level fallback; a fronting Cloudflare Access policy can gate it instead.
BASIC_USER = os.getenv("LIVE_USER")
BASIC_PASS = os.getenv("LIVE_PASS")

# Auto-reload interval (seconds) baked into every page so the bare URL refreshes
# itself (handy for a "watch it land" demo without appending ?refresh=). 0 = off
# (the local default). A per-request ?refresh=N still overrides; ?refresh=0 off.
DEFAULT_REFRESH = int(os.getenv("LIVE_REFRESH", "0") or "0")


def _resolve_asof(params: dict) -> date:
    """As-of date for the day-windows. ?asof= wins; else today + ?days (default 6)."""
    if params.get("asof"):
        return date.fromisoformat(params["asof"][0])
    days = DEFAULT_DAYS_AHEAD
    if params.get("days"):
        days = int(params["days"][0])
    return date.today() + timedelta(days=days)


def render(client_slug: str, asof: date) -> str:
    """Run the engine fresh and return the self-contained dashboard HTML string.

    Mirrors engine.write_dashboard but returns the string instead of writing a
    file, and skips the Mailchimp status read (like --skip-status) so a page load
    is fast and works even if creds/network are unavailable."""
    client = load_client(client_slug)
    ds = ingest.load(client)
    survey_result = survey.apply(ds, client)          # reads the live Form sheet
    log = outreach.load(client)
    outreach.tighten_survey_sent(ds, log)

    suppressed_out: list = []
    queue = engine.build_queue(ds, client, asof, log=log,
                               unsubscribed=set(), suppressed_out=suppressed_out)
    generated_at = datetime.now().isoformat(timespec="seconds")
    payload = engine.build_payload(ds, queue, client, asof, "dry-run",
                                   generated_at, survey_result, suppressed_out)

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
            self.send_header("WWW-Authenticate", 'Basic realm="SHIFT live demo"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if parsed.path == "/favicon.ico":
            self._send(204, "text/plain", b"")
            return

        slug = params.get("client", [self.server.default_client])[0]
        try:
            asof = _resolve_asof(params)
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
