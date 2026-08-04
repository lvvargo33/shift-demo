"""Daily auto-send: tag today's queue in Mailchimp so the Journeys send.

This is the hands-off counterpart to the local `python -m nudge_tool run --live`.
It runs on a schedule (Render cron), AFTER beta_pull has refreshed the Beta data,
and does the one thing that actually starts a follow-up: push each climber's tag
to Mailchimp. A tag-triggered Mailchimp Journey then sends the email. We never
send the email ourselves.

Flow each run:
  1. (cloud) pull the canonical Beta CSVs + the outreach log from Drive
  2. build today's queue (same engine as the dashboard: all precedence rules,
     once-only/cooldown from the outreach log, unsubscribed from Mailchimp,
     shared-inbox dedup)
  3. IF cleared to send -> tag each real climber (skip MANUAL safety + blanks),
     append the sends to the outreach log
  4. (cloud) push the updated outreach log back to Drive (so tomorrow's run and
     the dashboard export see today's sends)
  5. (optional) email a SUCCESS/FAILURE report

GATED OFF BY DEFAULT. Sending happens only when BOTH hold:
  - --i-have-shift-signoff is passed (baked into the cron start command), AND
  - the client config has live_enabled:true OR env NUDGE_LIVE_ENABLED is truthy.
The cloud bundle ships live_enabled:false, so the go-live switch is a single
Render env var, NUDGE_LIVE_ENABLED=true. No rebuild. Until then every run is a
safe no-op: it computes "would send N", tags nobody, and reports.

BEFORE flipping NUDGE_LIVE_ENABLED on, the real-climber go-live gates still apply
(SHIFT consent, the welcome-on-subscribe automation handled, the Journey built,
copy approved). This script tags; it does not check those.

Secrets / config (env, or .env next to this file for local runs):
  MAILCHIMP_API_KEY, MAILCHIMP_SERVER, MAILCHIMP_AUDIENCE_ID   the send account
  NUDGE_LIVE_ENABLED         "true"/"1" to actually send (default off = no-op)
  NUDGE_CLIENT               client slug (default "shift")

  # Drive round-trip (cloud cron). Beta CSV ids reuse beta_pull's env.
  BETA_DRIVE_TX, BETA_DRIVE_SES, BETA_DRIVE_MEM   Beta CSV Drive file ids
  BETA_TX_PATH, BETA_SES_PATH, BETA_MEM_PATH      working paths (= client data paths)
  OUTREACH_DRIVE_ID          Drive file id of the canonical outreach_log.csv
  GSHEETS_SA_B64 / _KEY / _JSON   service-account key (Editor on the Drive folder)

  # Email report (Gmail SMTP), reused from beta_pull.
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, NOTIFY_TO

Usage:
  py send_nudges.py                                   # local dry no-op + report
  py send_nudges.py --i-have-shift-signoff --yes      # local: send (if live_enabled)
  py send_nudges.py --drive --notify --yes --i-have-shift-signoff   # cloud cron
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import traceback
from datetime import date

from dotenv import load_dotenv

from pathlib import Path

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env", override=False)

from nudge_tool import engine, ingest, livesend, outreach, survey  # noqa: E402
from nudge_tool.config import load_client, load_settings  # noqa: E402
from nudge_tool.mailchimp_client import MailchimpClient, MailchimpError  # noqa: E402
from nudge_tool.stage import stage, versions  # noqa: E402
from requests.exceptions import RequestException  # noqa: E402


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def _pull_outreach_log(client) -> None:
    """Pull the canonical outreach log from Drive into the client's path (cloud).
    Missing id = skip (local runs use the on-disk log)."""
    fid = os.getenv("OUTREACH_DRIVE_ID")
    if not fid:
        print("  (no OUTREACH_DRIVE_ID; using the local outreach log)")
        return
    from nudge_tool import drive_io
    drive_io.pull(fid, str(client.outreach_log), svc=drive_io._service(drive_io._RO))
    print(f"  pulled outreach log -> {client.outreach_log} "
          f"({outreach.load(client).total_rows} prior send(s))")


def _push_outreach_log(client) -> None:
    """Push the updated outreach log back to its Drive file id (cloud)."""
    fid = os.getenv("OUTREACH_DRIVE_ID")
    if not fid:
        return
    if not client.outreach_log.exists():
        print("  (no local outreach log to push)")
        return
    from nudge_tool import drive_io
    drive_io.push(str(client.outreach_log), file_id=fid, svc=drive_io._service(drive_io._RW))
    print(f"  pushed outreach log -> {fid}")


def run_send(a) -> None:
    """The actual work. Prints a human report; raises on any failure."""
    slug = a.client or os.getenv("NUDGE_CLIENT", "shift")
    stage(f"run start: client={slug} drive={a.drive} yes={a.yes}")
    stage(f"libs: {versions()}")
    client = load_client(slug)
    live_ok = client.live_enabled or _truthy(os.getenv("NUDGE_LIVE_ENABLED"))
    today = date.fromisoformat(a.as_of) if a.as_of else date.today()

    print(f"Send nudges | client={slug} | as-of {today.isoformat()} | "
          f"live_enabled(cfg)={client.live_enabled} "
          f"NUDGE_LIVE_ENABLED={_truthy(os.getenv('NUDGE_LIVE_ENABLED'))} "
          f"signoff={a.i_have_shift_signoff} yes={a.yes}")

    if a.drive:
        print("\n=== DRIVE PULL (Beta CSVs + outreach log) ===")
        import beta_pull
        beta_pull.drive_pull_canonical()
        stage("drive pull done: Beta CSVs")
        _pull_outreach_log(client)
        stage("drive pull done: outreach log")

    # Build today's queue exactly as the dashboard does.
    ds = ingest.load(client)
    stage(f"ingest.load done ({len(ds.climbers)} climbers)")
    survey_result = survey.apply(ds, client)
    log = outreach.load(client)
    outreach.tighten_survey_sent(ds, log)
    stage(f"survey + outreach log done ({log.total_rows} prior sends)")

    # Unsubscribed/cleaned must be excluded before any send. If we mean to send
    # but can't read that list, abort rather than risk tagging a suppressed email.
    will_send = bool(a.i_have_shift_signoff and live_ok and a.yes)
    unsubscribed: set[str] = set()
    try:
        settings = load_settings(client, require=True)
        unsubscribed = MailchimpClient(settings).suppressed_emails()
        print(f"  mailchimp: {len(unsubscribed)} unsubscribed/cleaned (excluded)")
        stage(f"mailchimp unsub list done ({len(unsubscribed)} suppressed)")
    except (RuntimeError, MailchimpError, RequestException) as e:
        if will_send:
            raise RuntimeError(
                f"refusing to send: could not read Mailchimp unsub list ({e}). "
                "Fix creds/connectivity; not tagging without it.") from e
        print(f"  NOTE  no Mailchimp status read ({e}); fine for a no-op/preview run.")

    asof = engine.resolve_asof(ds, a.as_of) if a.as_of else today
    suppressed_out: list = []
    queue = engine.build_queue(ds, client, asof, log=log, unsubscribed=unsubscribed,
                               suppressed_out=suppressed_out, dedup_email=True,
                               own_email_guard=True)
    targets = livesend.derive_sends(queue)
    manual = sum(1 for q in queue if q.trigger_name == "MANUAL")
    stage(f"queue built (queue={len(queue)}, to-send={len(targets)})")

    print(f"\n  queue={len(queue)}  to-send(auto-email)={len(targets)}  "
          f"safety/MANUAL={manual} (manager call, never auto-emailed)")
    for t in targets:
        print(f"    {t.tag:<16} {t.name:<22.22} {t.email}")

    # --- decide: send, or one of the safe no-op / preview paths ---
    if not a.i_have_shift_signoff:
        print("\n  NO-OP: --i-have-shift-signoff not passed. Tagged nobody.")
        stage("no-op: no signoff flag; tagged nobody")
        return
    if not live_ok:
        print("\n  GATED OFF: live sending is not enabled (client live_enabled=false "
              "and NUDGE_LIVE_ENABLED not set).\n  Computed the queue and tagged "
              "NOBODY. Flip NUDGE_LIVE_ENABLED=true to go live (after SHIFT signoff "
              "+ welcome-email handled + Journey + copy).")
        stage("gated off: live sending not enabled; tagged nobody")
        return
    if not a.yes:
        print(f"\n  PREVIEW only (no --yes): would tag {len(targets)} climber(s). "
              "Tagged nobody.")
        stage("preview only (no --yes); tagged nobody")
        return
    if not targets:
        print("\n  LIVE: nothing to tag this run.")
        stage("live: nothing to tag this run")
        return

    mc = MailchimpClient(settings)
    print(f"\n  LIVE: tagging {len(targets)} climber(s) ...")
    stage(f"tagging {len(targets)} climber(s) in Mailchimp ...")
    tagged: list = []
    for i, t in enumerate(targets, 1):
        try:
            mc.tag_member(t.email, t.first_name, t.tag)
            tagged.append(t)
            print(f"    ok    {t.tag:<16} {t.email}")
        except (MailchimpError, RequestException) as e:
            print(f"    FAIL  {t.tag:<16} {t.email}  ({e})")
        if i % 10 == 0:
            stage(f"tagging {i}/{len(targets)}")
    written = outreach.append(client, livesend.log_rows(tagged, asof))
    print(f"\n  tagged {len(tagged)} climber(s), logged {written} row(s).")
    stage(f"tagging done ({len(tagged)} tagged, {written} row(s) logged)")

    if a.drive:
        print("\n=== DRIVE PUSH (outreach log) ===")
        _push_outreach_log(client)
        stage("drive push done: outreach log")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Tag today's nudge queue in Mailchimp (gated; default no-op).")
    ap.add_argument("--client", default=None, help="client slug (default: NUDGE_CLIENT or 'shift')")
    ap.add_argument("--as-of", default=None, help="YYYY-MM-DD; default today")
    ap.add_argument("--drive", action="store_true",
                    help="cloud mode: pull Beta CSVs + outreach log from Drive, push log back")
    ap.add_argument("--i-have-shift-signoff", action="store_true",
                    help="required to send (matches the --live lock)")
    ap.add_argument("--yes", action="store_true",
                    help="actually tag; without it the run previews only")
    ap.add_argument("--notify", action="store_true",
                    help="email a success/failure report (needs SMTP_* env)")
    a = ap.parse_args()

    buf = io.StringIO()
    status = "SUCCESS"
    try:
        with contextlib.redirect_stdout(buf):
            run_send(a)
    except BaseException:  # noqa: BLE001 - any failure must still notify
        status = "FAILURE"
        buf.write("\n\n*** SEND RUN FAILED ***\n")
        buf.write(traceback.format_exc())
        stage("run_send FAILED (traceback is in the report email)")

    report = buf.getvalue()
    print(report)
    stage(f"send phase finished: {status}")

    # Refresh the shared "Send It - All Gyms" stats sheet after the send run.
    # Guarded: a stats failure must never fail (or re-run) the send itself.
    # Output is captured into the report so its notes (e.g. Experiments
    # auto-fill skips, S40) reach the emailed report too.
    if status == "SUCCESS":
        buf2 = io.StringIO()
        stage("allgyms stats push: start")
        try:
            with contextlib.redirect_stdout(buf2):
                import allgyms_push
                allgyms_push.push(a.client or os.getenv("NUDGE_CLIENT",
                                                        "shift"))
            stage("allgyms stats push: done")
        except Exception:
            buf2.write("  allgyms stats push FAILED (send run unaffected):\n"
                       + traceback.format_exc())
            stage("allgyms stats push FAILED (send run unaffected)")
        print(buf2.getvalue())
        report += "\n=== ALL GYMS STATS PUSH ===\n" + buf2.getvalue()

    if a.notify:
        import beta_pull
        when = getattr(__import__("refresh_beta_data"), "STAMP", "")
        if status == "SUCCESS":
            subject = f"SUCCESS - nudge send run ({when})"
            banner = ("==================================================\n"
                      "  SUCCESS - nudge send run complete.\n"
                      "==================================================\n\n")
        else:
            subject = f"FAILURE - nudge send run FAILED ({when})"
            banner = ("==================================================\n"
                      "  FAILURE - the nudge send run errored.\n"
                      "  ACTION: check the logs; some climbers may not be tagged.\n"
                      "==================================================\n\n")
        try:
            beta_pull.send_email(subject, banner + report)
            stage("report email sent")
        except Exception:
            print("  EMAIL SEND FAILED:\n" + traceback.format_exc())
            stage("report email FAILED to send")
            status = "FAILURE"

    stage(f"run complete: {status}")
    sys.exit(0 if status == "SUCCESS" else 1)


if __name__ == "__main__":
    main()
