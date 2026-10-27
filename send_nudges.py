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

from nudge_tool import engine, ingest, livesend, member_checkin, outreach, survey, survey_dispatch  # noqa: E402
from nudge_tool.config import load_client, load_settings  # noqa: E402
from nudge_tool.mailchimp_client import MailchimpClient, MailchimpError  # noqa: E402
from nudge_tool.stage import stage, versions  # noqa: E402
from requests.exceptions import RequestException  # noqa: E402


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


# Per-email send failures (CLAUDE.md rule 5). Each send is caught on its own
# row so one bad address cannot stop the rest, but that left the report at a
# plain SUCCESS even when nobody got an email (2026-10-27: Brevo outage, 15 of
# 15 ABC sends failed, subject said SUCCESS). run_send counts here; main() puts
# the count in the subject and the banner.
_SEND_TALLY: dict = {"tried": 0, "failed": 0}


def _print_send_failure(tag: str, email: str, detail: str) -> None:
    _SEND_TALLY["failed"] += 1
    print(f"    FAILED {tag:<16} {email}  {detail}")


def _failure_notice(gym: str) -> tuple[str, str]:
    """(subject prefix, banner lines) for a SUCCESS run. Failed sends are
    never logged, so each is retried next run while the person is still due."""
    tried, failed = _SEND_TALLY["tried"], _SEND_TALLY["failed"]
    if not failed:
        return "SUCCESS", f"  SUCCESS - {gym}nudge send run complete.\n"
    return (f"SUCCESS ({failed} of {tried} sends FAILED)",
            f"  SUCCESS - {gym}nudge send run complete, BUT\n"
            f"  FAILED: {failed} of {tried} email(s) did not send (lines marked FAILED\n"
            f"  below). They were not logged, so each is retried next run while\n"
            f"  still due.\n")


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


def _send_member_checkins(client, mc, cfg: dict, due: list, asof) -> None:
    """Tag the day-14 check-ins (only called on a live run with the switch
    on). Runs BEFORE the first-timer sends so a Brevo or survey problem there
    cannot hold them up, and never raises, so it cannot hold those up either.
    Each tag that lands is logged even if a later one fails; a failure prints
    FAILED and is retried next run (inside late_days). A member Mailchimp
    already shows tagged this membership is not re-tagged; their lost log
    row is written back."""
    if not due:
        return
    try:
        tag = cfg.get("tag") or member_checkin.DEFAULTS["tag"]
        print(f"\n  LIVE: member check-in, tagging {len(due)} member(s) ...")
        _SEND_TALLY["tried"] += len(due)
        sent: list = []
        for c in due:
            try:
                how = member_checkin.send(mc, c, tag)
            except Exception as e:  # noqa: BLE001
                _print_send_failure(tag, c.email, "member check-in NOT tagged, "
                                    f"retried next run ({type(e).__name__}: {e})")
                continue
            sent.append(c)
            print(f"    ok    {tag:<16} {c.email}" + (
                "  (already tagged in Mailchimp; log row restored, no second email)"
                if how == "already" else ""))
        written = outreach.append(client, member_checkin.log_rows(sent, asof, tag))
        print(f"  member check-in: tagged {len(sent)}, logged {written} row(s).")
        stage(f"member check-in done ({len(sent)} tagged)")
    except Exception:  # noqa: BLE001
        print("    member check-in FAILED (first-timer sends unaffected; Mailchimp's tag "
              "dates stop a second email next run):\n" + traceback.format_exc())
        stage("member check-in FAILED")


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

    # Residency pre-pass (trial-swap 2026-08-07): zips for the residency-gated
    # triggers, attached BEFORE the queue builds. Shared with the local dry run
    # since 2026-08-13 so the two paths cannot disagree; see
    # residency.attach_for_active_triggers for why.
    from nudge_tool import residency
    n = residency.attach_for_active_triggers(ds, client, asof, log)
    stage(f"residency zips attached ({n} fetched live)")

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
        route = survey_dispatch.route_for(client, t)
        via = {"native": "  [Brevo]", "hold": "  [HOLD]"}.get(route, "")
        print(f"    {t.tag:<16} {t.name:<22.22} {t.email}{via}")

    # Day-14 member check-in (Block 25 Part B): its own path off the plan rows,
    # since build_queue skips every member. Switched off = preview lines only.
    # Guarded: a failure here must never stop the first-timer sends (rule 5).
    ci_cfg: dict = dict(member_checkin.DEFAULTS)
    ci_live = False
    ci_due: list = []
    try:
        ci_cfg = member_checkin.load_cfg(client)
        ci_live = member_checkin.is_live(ci_cfg)
        ci = member_checkin.find(
            client.memberships_csv, asof, ci_cfg, log=log, unsubscribed=unsubscribed,
            staff_emails=member_checkin.staff_emails_of(client, ds),
            # inboxes that really get a first-timer email today (a "hold"
            # route never sends, so it must not hold a check-in day after day)
            skip_emails={t.email for t in targets
                         if survey_dispatch.route_for(client, t) != "hold"},
            min_days_between=client.min_days_between_sends, ignore_go_live=not ci_live)
        ci_due = ci.due
        print()
        for line in member_checkin.report_lines(ci, ci_cfg, ci_live, asof):
            print(line)
        stage(f"member check-in: {len(ci_due)} due (live={ci_live})")
    except Exception:  # noqa: BLE001
        print("\n  member check-in FAILED (first-timer sends unaffected):\n"
              + traceback.format_exc())
        stage("member check-in FAILED")
    if not ci_live:
        ci_due = []

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
    if not targets and not ci_due:
        print("\n  LIVE: nothing to tag this run.")
        stage("live: nothing to tag this run")
        return

    mc = MailchimpClient(settings)
    _send_member_checkins(client, mc, ci_cfg, ci_due, asof)
    if not targets:
        print("\n  LIVE: no first-timer emails this run.")
        if a.drive:
            print("\n=== DRIVE PUSH (outreach log) ===")
            _push_outreach_log(client)
            stage("drive push done: outreach log")
        return
    # Survey emails go through the dispatch switch (Block 25 A1): legacy =
    # Mailchimp tag as always, native = Brevo, hold = nobody. prepare() reads
    # the opt-out + Brevo block lists first and RAISES if it can't, which
    # fails this run before anything is sent (decision 3, 2026-10-23).
    disp = survey_dispatch.Dispatcher(client, mc, entrypoint="scheduler", asof=asof)
    for line in disp.prepare(targets):
        print(line)
    stage("survey dispatch prepared")
    print(f"\n  LIVE: tagging {len(targets)} climber(s) ...")
    stage(f"tagging {len(targets)} climber(s) in Mailchimp ...")
    tagged: list = []
    _SEND_TALLY["tried"] += len(targets)
    for i, t in enumerate(targets, 1):
        try:
            o = disp.send(t)
        except (MailchimpError, RequestException) as e:
            _print_send_failure(t.tag, t.email, f"({e})")
        else:
            if o.sent:
                tagged.append(t)
                print(f"    ok    {t.tag:<16} {t.email}"
                      + ("  (Brevo)" if o.transport == "brevo" else ""))
            elif o.state == "failed":
                _print_send_failure(t.tag, t.email, "survey email NOT sent, "
                                    f"retried next run ({o.detail})")
            else:
                print(f"    skip  {t.tag:<16} {t.email}  ({o.state}"
                      + (f": {o.detail}" if o.detail else "") + ")")
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
            head, lines = _failure_notice("")
            subject = f"{head} - nudge send run ({when})"
            banner = ("==================================================\n"
                      + lines +
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
