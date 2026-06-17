"""shift-nudge-tool command line.

Commands:
  selftest   Read-only connection check against a client's Mailchimp. Touches
             nothing.
  run        Compute today's nudge queue and (depending on mode) tag climbers.

Modes (apply to `run`):
  --dry-run  DEFAULT. Compute + report, tag nobody. Safe always.
  --test     Tag only plus-addressed dummy contacts (wired in T4).
  --live     Real upsert + tag against the real audience. Triple-locked:
             (1) refuses without --i-have-shift-signoff,
             (2) refuses unless the client config has live_enabled: true,
             (3) real tagging itself lands in T4 behind an interactive confirm.

Pick the client with --client <slug> (default: shift).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

from requests.exceptions import RequestException

from . import config, engine, ingest, livesend, outreach, survey, testmode
from .config import load_client, load_settings
from .mailchimp_client import MailchimpClient, MailchimpError


def cmd_selftest(args: argparse.Namespace) -> int:
    print("Send It :: connection self-test (read-only)\n")
    try:
        client = load_client(args.client)
    except RuntimeError as e:
        print(f"  FAIL  client config: {e}  (known: {config.list_clients()})")
        return 1
    print(f"  ok    client '{client.client_name}' "
          f"(slug={client.slug}, live_enabled={client.live_enabled})")
    try:
        settings = load_settings(client, require=True)
    except RuntimeError as e:
        print(f"  FAIL  credentials: {e}")
        return 1
    print(f"  ok    env loaded  (server={settings.server}, "
          f"audience={settings.audience_id})")

    mc = MailchimpClient(settings)
    try:
        acct = mc.ping()
    except MailchimpError as e:
        print(f"  FAIL  ping: {e}")
        return 1
    except RequestException as e:
        print(f"  FAIL  ping (network): {e}")
        return 1
    print(f"  ok    ping  -> account '{acct.get('account_name', '?')}' "
          f"(login {acct.get('login_id', '?')})")

    try:
        aud = mc.get_audience()
    except MailchimpError as e:
        print(f"  FAIL  audience: {e}")
        return 1
    stats = aud.get("stats", {})
    print(f"  ok    audience '{aud.get('name', '?')}'  "
          f"members={stats.get('member_count', '?')} "
          f"unsub={stats.get('unsubscribe_count', '?')}")

    print("\n  PASS  Mailchimp reachable and authenticated. No data written.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    mode = "live" if args.live else "test" if args.test else "dry-run"
    print(f"Send It :: run (client={args.client}, mode={mode})\n")

    try:
        client = load_client(args.client)
    except RuntimeError as e:
        print(f"  FAIL  client config: {e}  (known: {config.list_clients()})")
        return 1

    if args.live:
        if not args.i_have_shift_signoff:
            print("  REFUSED  --live needs --i-have-shift-signoff. No --live run "
                  "may touch real\n           climbers until the client clears "
                  "consent + the Journey + copy.")
            return 2
        if not client.live_enabled:
            print(f"  REFUSED  client '{client.slug}' has live_enabled=false. "
                  "Flip it to true in\n           client.json only after signoff. "
                  "(Structural lock; intentional.)")
            return 2

    # --- compute the queue (the brain; identical across modes) ---
    print("  reading Beta exports ...")
    ds = ingest.load(client)
    survey_result = survey.apply(ds, client)  # no-op unless survey.enabled
    if client.survey.get("enabled"):
        print(f"  survey: matched={survey_result.matched} "
              f"unmatched={survey_result.unmatched} "
              f"safety={survey_result.safety_count} "
              f"blockers={survey_result.by_blocker or '{}'}")

    # S3 rule 4: the local outreach log (once-only / cooldown). Tightens the
    # survey_sent anchor to the real survey_request send date when we have one.
    log = outreach.load(client)
    tightened = outreach.tighten_survey_sent(ds, log)
    if log.total_rows:
        print(f"  outreach log: {log.total_rows} prior send(s) on file"
              + (f"; survey_sent anchor tightened for {tightened}" if tightened else ""))

    # S3 rule 5: Mailchimp unsubscribe/cleaned status (read-only). Graceful: if
    # creds are missing, the read fails, or --skip-status is set, we warn and
    # suppress nobody (dry-run still works fully offline).
    unsubscribed: set[str] = set()
    if not args.skip_status:
        try:
            settings = load_settings(client, require=True)
            unsubscribed = MailchimpClient(settings).suppressed_emails()
            print(f"  mailchimp status: {len(unsubscribed)} unsubscribed/cleaned "
                  "(will not be tagged)")
        except (RuntimeError, MailchimpError, RequestException) as e:
            print(f"  NOTE  skipping Mailchimp status read ({e}); "
                  "suppressing nobody on unsub. Use --skip-status to silence.")

    asof = engine.resolve_asof(ds, args.as_of)
    suppressed_out: list = []
    queue = engine.build_queue(ds, client, asof, log=log,
                               unsubscribed=unsubscribed,
                               suppressed_out=suppressed_out,
                               dedup_email=True, own_email_guard=True)
    generated_at = datetime.now().isoformat(timespec="seconds")
    payload = engine.build_payload(ds, queue, client, asof, mode, generated_at,
                                   survey_result, suppressed_out,
                                   sent_rows=outreach.load_rows(client))
    out_path = engine.write_data_json(payload, client)
    html_path = engine.write_dashboard(payload, client)

    s = payload["summary"]
    print(f"  as-of {asof.isoformat()}   "
          f"(tx_max={ds.tx_max_date}, sessions_max={ds.sessions_max_date})")
    print(f"  trial buyers={s['trial_buyers']}  "
          f"one-and-done(non-member)={s['one_and_done_not_member']}  "
          f"ELIGIBLE NOW={s['eligible_now']}")
    print(f"  data.json  -> {out_path}")
    print(f"  dashboard  -> {html_path}\n")

    bt = s.get("by_trigger", {})
    nonzero = {k: v for k, v in bt.items() if v}
    if nonzero:
        print("  by trigger: " + ", ".join(f"{k}={v}" for k, v in nonzero.items()))

    sup = payload.get("suppression", {})
    if sup.get("total"):
        reasons = ", ".join(f"{k}={v}" for k, v in sup["by_reason"].items())
        print(f"  suppressed (matched but withheld): {sup['total']}  [{reasons}]")

    if queue:
        print("  Recovery Queue (priority / name / trigger / anchor+day / visits / tag / email):")
        for q in queue:
            print(f"    {q.priority:>2}. {q.name:<22.22} {q.trigger_name:<16.16} "
                  f"{q.anchor}+d{q.days_since} v{q.visit_count}  "
                  f"{q.tag:<14} {q.email}")
    else:
        print("  Recovery Queue is empty for this as-of date.")

    print()
    if mode == "dry-run":
        print("  DRY-RUN: computed + wrote data.json. Tagged nobody.")
        return 0
    if mode == "test":
        return _run_test_mode(args, client, queue, log, asof)
    if mode == "live":
        return _run_live_mode(args, client, queue, asof)
    return 0


def _run_live_mode(args, client, queue, asof) -> int:
    """Tag the REAL climbers in the final queue so the Mailchimp Journey sends.

    The queue is already the safe list (every precedence rule + shared-inbox
    dedup ran in build_queue). Gates above already required --i-have-shift-signoff
    and live_enabled=true. Like --test, this PREVIEWS without --yes and only
    writes with --yes."""
    targets = livesend.derive_sends(queue)
    manual = [q for q in queue if q.trigger_name == "MANUAL"]
    if manual:
        print(f"  {len(manual)} safety (MANUAL) item(s) -> manager call, never "
              "auto-emailed; skipped here.")
    if not targets:
        print("  LIVE: nothing to send (queue has no auto-email climbers this run).")
        return 0

    print(f"  LIVE: {len(targets)} climber(s) cleared to tag "
          f"(survey/cooldown/unsub already filtered):")
    for t in targets:
        print(f"    {t.tag:<16} {t.name:<22.22} {t.email}")

    if not args.yes:
        print(f"\n  PREVIEW only. Tagged NOBODY. {len(targets)} climber(s) would "
              "be tagged in Mailchimp.\n  Re-run with --live --i-have-shift-signoff "
              "--yes to actually push the tags.")
        return 0

    try:
        settings = load_settings(client, require=True)
    except RuntimeError as e:
        print(f"  FAIL  credentials needed to tag climbers: {e}")
        return 2
    mc = MailchimpClient(settings)
    print(f"\n  --yes: tagging {len(targets)} climber(s) in Mailchimp ...")
    tagged: list = []
    for t in targets:
        try:
            mc.tag_member(t.email, t.first_name, t.tag)
            tagged.append(t)
            print(f"    ok    {t.tag:<16} {t.email}")
        except (MailchimpError, RequestException) as e:
            print(f"    FAIL  {t.tag:<16} {t.email}  ({e})")
    written = outreach.append(client, livesend.log_rows(tagged, asof))
    print(f"\n  LIVE done: tagged {len(tagged)} climber(s), logged {written} row(s) "
          "to the outreach log.")
    return 0


def _run_test_mode(args, client, queue, log, asof) -> int:
    """S5: tag plus-addressed dummies (never a real climber). Without --yes this
    only previews; with --yes it upserts + tags the dummies and logs them."""
    base = (client.test_contacts.get("base_email") or "").strip()
    targets = testmode.derive_targets(queue, client)
    if not targets:
        if not base:
            print("  TEST mode: no test_contacts.base_email configured in "
                  "client.json. Nothing to tag.")
        else:
            print("  TEST mode: queue is empty, so there are no dummies to tag.")
        return 0

    # Independent fail-closed safety gate: every target must be a plus-variant of
    # base_email. A real address here aborts the whole run before any write.
    try:
        testmode.assert_all_dummy(targets, base)
    except testmode.TestSafetyError as e:
        print(f"  REFUSED  {e}")
        return 2

    to_send, suppressed = testmode.filter_already_sent(targets, log, client, asof)

    print(f"  TEST mode: {len(targets)} dummy contact(s) derived from {base}")
    print("            (one per distinct tag in the queue; all mail lands in that "
          "inbox).")
    for t in targets:
        print(f"    {t.tag:<16} <- stands in for {t.real_name:<22.22} -> {t.dummy_email}")
    if suppressed:
        print("  already sent (once-only/cooldown on the dummy; will NOT re-tag):")
        for t, reason in suppressed:
            print(f"    {t.tag:<16} {t.dummy_email}  [{reason}]")

    if not to_send:
        print("\n  Nothing new to tag (all dummies already sent). "
              "Run `cleanup` to reset them.")
        return 0

    if not args.yes:
        print(f"\n  PREVIEW only — tagged NOBODY. {len(to_send)} dummy contact(s) "
              "would be upserted + tagged.\n  Re-run with --test --yes to actually "
              "write these dummies to Mailchimp.")
        return 0

    # --yes: actually write. Dummies only — the assert above guarantees it.
    try:
        settings = load_settings(client, require=True)
    except RuntimeError as e:
        print(f"  FAIL  credentials needed to tag dummies: {e}")
        return 2
    mc = MailchimpClient(settings)
    print(f"\n  --yes: tagging {len(to_send)} dummy contact(s) in Mailchimp ...")
    tagged: list = []
    for t in to_send:
        try:
            mc.tag_member(t.dummy_email, "SHIFT Test", t.tag)
            tagged.append(t)
            print(f"    ok    {t.tag:<16} {t.dummy_email}")
        except (MailchimpError, RequestException) as e:
            print(f"    FAIL  {t.tag:<16} {t.dummy_email}  ({e})")
    written = outreach.append(client, testmode.log_rows(tagged, asof))
    print(f"\n  TEST done: tagged {len(tagged)} dummy(ies), logged {written} row(s) "
          "to the outreach log.")
    print("  Re-run `--test` to see once-only/cooldown suppression; run "
          "`cleanup --client " + client.slug + "` to remove the dummies.")
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    """Remove the test dummies: untag + archive every mode=test contact in the
    outreach log, then drop those rows so the log reflects real sends only."""
    print(f"Send It :: cleanup test dummies (client={args.client})\n")
    try:
        client = load_client(args.client)
    except RuntimeError as e:
        print(f"  FAIL  client config: {e}  (known: {config.list_clients()})")
        return 1

    rows = outreach.load_rows(client)
    test_rows = [r for r in rows if (r.get("mode") or "").strip() == "test"]
    keep_rows = [r for r in rows if (r.get("mode") or "").strip() != "test"]
    if not test_rows:
        print("  Nothing to clean: no mode=test rows in the outreach log.")
        return 0

    # email -> set of tags applied to it (untag each before archiving)
    by_email: dict[str, set[str]] = {}
    for r in test_rows:
        by_email.setdefault(r["email"], set()).add(r["tag"])

    print(f"  {len(test_rows)} test row(s) across {len(by_email)} dummy contact(s):")
    for email, tags in by_email.items():
        print(f"    {email}  tags={sorted(tags)}")

    if not args.yes:
        print("\n  PREVIEW only — removed nothing. Re-run `cleanup --yes` to untag "
              "+ archive these dummies\n  and drop their rows from the outreach log.")
        return 0

    try:
        settings = load_settings(client, require=True)
    except RuntimeError as e:
        print(f"  FAIL  credentials needed to clean up: {e}")
        return 2
    mc = MailchimpClient(settings)
    print("\n  --yes: untagging + archiving dummies ...")
    failures = 0
    for email, tags in by_email.items():
        for tag in sorted(tags):
            try:
                mc.remove_tag(email, tag)
            except (MailchimpError, RequestException) as e:
                failures += 1
                print(f"    warn  remove_tag {tag} on {email}: {e}")
        try:
            mc.archive_member(email)
            print(f"    ok    archived {email}")
        except (MailchimpError, RequestException) as e:
            failures += 1
            print(f"    warn  archive {email}: {e}")

    outreach.rewrite(client, keep_rows)
    print(f"\n  Cleanup done: archived {len(by_email)} dummy(ies), kept "
          f"{len(keep_rows)} non-test log row(s).")
    if failures:
        print(f"  {failures} Mailchimp warning(s) above (often a dummy that was "
              "already archived) — log rows were still dropped.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="shift-nudge-tool")
    sub = p.add_subparsers(dest="command", required=True)

    st = sub.add_parser("selftest", help="read-only Mailchimp connection check")
    st.add_argument("--client", default="shift", help="client slug (default: shift)")

    run = sub.add_parser("run", help="compute the send queue")
    run.add_argument("--client", default="shift", help="client slug (default: shift)")
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="default: compute + report, tag nobody")
    mode.add_argument("--test", action="store_true",
                      help="tag only plus-addressed dummy contacts")
    mode.add_argument("--live", action="store_true",
                      help="real tagging (gated: signoff flag + live_enabled)")
    run.add_argument("--i-have-shift-signoff", action="store_true",
                     help="explicit confirmation required for --live")
    run.add_argument("--as-of", metavar="YYYY-MM-DD", default=None,
                     help="reference date for the day-window "
                          "(default: latest transaction date in the export)")
    run.add_argument("--skip-status", action="store_true",
                     help="skip the read-only Mailchimp unsubscribe-status check "
                          "(stay fully offline; suppress nobody on unsub)")
    run.add_argument("--yes", action="store_true",
                     help="in --test, actually tag the dummy contacts (without it, "
                          "--test only previews and writes nothing)")

    cl = sub.add_parser("cleanup",
                        help="untag + archive the --test dummy contacts")
    cl.add_argument("--client", default="shift", help="client slug (default: shift)")
    cl.add_argument("--yes", action="store_true",
                    help="actually untag + archive (without it, preview only)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "selftest":
        return cmd_selftest(args)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "cleanup":
        return cmd_cleanup(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
