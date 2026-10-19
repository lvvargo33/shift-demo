"""Auto-pull Beta exports via Beta's internal API, no manual export, no browser.

Replaces the "log into Beta and click export" step. Beta's web app authenticates
with Firebase; a short-lived (1 hour) ID token signs each API call. We mint that
token on the fly from a long-lived Firebase REFRESH token (captured once from the
browser), so this runs unattended on a schedule.

Flow each run:
  1. refresh token + API key  -> fresh 1-hour ID token (Google securetoken endpoint)
  2. (cloud) pull the canonical CSVs from Drive into the working dir
  3. ID token -> pull transactions + sessions for a rolling window, and the
     membership list twice: the ACTIVE list (for the daily snapshot) and the FULL
     list, cancelled ones included, so `cancelled_date` lands in Memberships.csv
  4. hand the CSVs to refresh_beta_data's tested merge/dedup/backup logic
  5. (cloud) push the merged canonical CSVs back to Drive so the dashboard updates
  6. append today's ACTIVE membership list to the daily snapshot file (guarded:
     a failure prints `member snapshot FAILED` and never stops steps 1-5)
  7. (optional) email a success/failure report

A rolling overlap window (default 2 days) means a missed or late-posting row is
always caught on the next run; the merge dedup removes the overlap. Transactions
use a longer window (--tx-days, default 90, since 2026-10-19) so a sale's later
payment, cancellation or refund replaces the saved copy.

Secrets / config (env or .env next to this file):
  BETA_REFRESH_TOKEN   long-lived Firebase refresh token (capture once from browser)
  BETA_API_KEY         Firebase web API key (AIza...)
  BETA_GYM_ID          Beta gym id (optional; default 13353 = SHIFT)

  # Drive round-trip (cloud cron). When these + GSHEETS_SA_JSON are set and --drive
  # is passed, the canonical files are pulled from / pushed back to Drive by id.
  BETA_DRIVE_TX, BETA_DRIVE_SES, BETA_DRIVE_MEM   Drive file ids (drive_manifest.json)
  BETA_TX_PATH, BETA_SES_PATH, BETA_MEM_PATH      where the canonical CSVs live (working dir)
  BETA_DRIVE_MEMSNAP   Drive file id of the daily active-member snapshot (--drive)
  BETA_MEMSNAP_PATH    its working path (default: next to Memberships.csv)

  # Email (Gmail SMTP). When set and --notify is passed, sends a daily report.
  SMTP_HOST (default smtp.gmail.com), SMTP_PORT (default 587)
  SMTP_USER, SMTP_PASS (Gmail app password), NOTIFY_TO (default = SMTP_USER)

Usage:
  py beta_pull.py                      # local: pull + merge into PC files
  py beta_pull.py --push <FOLDER_ID>   # local: also push canonical CSVs to Drive
  py beta_pull.py --drive --notify     # cloud: Drive pull -> merge -> Drive push + email
  py beta_pull.py --no-merge           # pull only, print counts, touch nothing
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import smtplib
import ssl
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
# Load .env BEFORE importing refresh_beta_data so its env-driven path constants
# (BETA_TX_PATH etc.) resolve correctly. On Render the vars are already in the
# environment, so order is harmless there.
load_dotenv(BASE / ".env", override=False)

import refresh_beta_data as rbd  # noqa: E402

# Default = next to Memberships.csv (SHIFT_Docs on the PC, beta_data/ on Render).
SNAP_DST = Path(os.getenv("BETA_MEMSNAP_PATH")
                or (rbd.MEM_DST.parent / "Beta_Active_Members_Daily.csv"))
SNAP_TZ = "America/New_York"  # SHIFT is West Michigan; the cron runs 3 AM ET

TOKEN_URL = "https://securetoken.googleapis.com/v1/token"
API = "https://api.sendmoregetbeta.com"
GYM = os.getenv("BETA_GYM_ID", "13353")
ORIGIN = "https://gym.sendmoregetbeta.com"


def _http(url: str, *, data: bytes | None = None, headers: dict | None = None,
          method: str | None = None) -> str:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"HTTP {e.code} from {url}\n  {body}") from e


def mint_token() -> str:
    """Long-lived refresh token -> fresh 1-hour Firebase ID token. No browser."""
    # Strip ALL whitespace, not just the ends: cloud env fields wrap long values
    # and can store real line breaks INSIDE the token. Tokens never contain
    # whitespace, so collapsing it is safe and undoes that corruption.
    import re
    api_key = re.sub(r"\s+", "", os.getenv("BETA_API_KEY", ""))
    refresh = re.sub(r"\s+", "", os.getenv("BETA_REFRESH_TOKEN", ""))
    missing = [n for n, v in (("BETA_API_KEY", api_key),
                              ("BETA_REFRESH_TOKEN", refresh)) if not v]
    if missing:
        raise RuntimeError(f"Missing secret(s): {', '.join(missing)} "
                           f"(set in {BASE / '.env'} or the environment).")
    # Diagnostic: a truncated paste is the usual cause of INVALID_REFRESH_TOKEN.
    # The known-good token is 204 chars; a different length means the env value
    # got cut or altered. (Length only -- the secret itself is never logged.)
    print(f"  refresh-token length: {len(refresh)} (expected 204), "
          f"api-key length: {len(api_key)} (expected 39)")
    body = urllib.parse.urlencode(
        {"grant_type": "refresh_token", "refresh_token": refresh}).encode()
    out = _http(f"{TOKEN_URL}?key={api_key}", data=body, method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"})
    tok = json.loads(out).get("id_token")
    if not tok:
        raise RuntimeError("Token refresh returned no id_token (refresh token may "
                           "be revoked: re-capture it from the browser).")
    return tok


def pull_transactions(token: str, start: str, end: str) -> str:
    body = json.dumps({
        "advancedOptions": {"creationTimeBetween": {"start": start, "end": end}},
        "downloadType": "transactions", "download": "transactions",
    }).encode()
    return _http(f"{API}/v2/pos/txBatch/{GYM}", data=body, method="POST",
                 headers={"Authorization": token,
                          "Content-Type": "text/plain;charset=UTF-8",
                          "Referer": f"{ORIGIN}/"})


TX_CHUNK_DAYS = 30   # one transactions call covers at most this many days


def pull_transactions_window(token: str, start: datetime, end: datetime) -> str:
    """Transactions created in start..end, fetched TX_CHUNK_DAYS at a time and
    joined into one CSV (header once). A sale is returned again on every
    pull while it is inside the window, in its CURRENT state, and the merge
    lets that copy replace the saved one: this is how a later payment, a
    cancellation or a refund on an older sale reaches the canonical file.
    Why a long window (2026-10-19, Jadee's September check): a refund landed
    43 days after its sale ($360 Homeschool, sold 7/21, refunded 9/2) and an
    unpaid autobill was paid 21 days later; the old 2-day window never saw
    either, so September read $111.53 high."""
    import csv
    header, body = None, []
    s = start
    while s < end:
        e = min(s + timedelta(days=TX_CHUNK_DAYS), end)
        text = pull_transactions(token, s.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                                 e.strftime("%Y-%m-%dT%H:%M:%S.000Z")).lstrip("﻿")
        rows = list(csv.reader(io.StringIO(text)))   # csv, not lines: a quoted field may hold a line break
        if rows:
            if header is None:
                header = rows[0]
            elif rows[0] != header:
                raise RuntimeError("transactions chunks came back with different headers")
            body.extend(r for r in rows[1:] if any(c.strip() for c in r))
        s = e
    if header is None:
        return ""
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(header)
    w.writerows(body)          # a sale on a chunk edge can appear twice; the merge keys on id
    return out.getvalue()


def pull_sessions(token: str, date_from: str, date_to: str) -> str:
    q = urllib.parse.urlencode(
        {"download": "true", "dateFrom": date_from, "dateTo": date_to})
    return _http(f"{API}/v3/{GYM}/session/list?{q}",
                 headers={"authorization": token, "accept": "*/*",
                          "origin": ORIGIN, "referer": f"{ORIGIN}/"})


def pull_memberships(token: str, *, all_states: bool = False) -> str:
    """Beta's subscription list.

    Default = `state=1,2`, the current (active) list the pull has always asked
    for; the daily snapshot uses it. `all_states=True` drops the filter: every
    membership Beta holds, cancelled ones included, each with `cancelled_date`
    filled (probe 2026-10-15: 477 rows, 273 cancelled, 204 active = the same
    204 the filtered call returns). The merge takes the full list so cancel
    dates land in Memberships.csv; before 2026-10-15 only the active list was
    pulled and the column stayed blank for anyone who cancelled after the first
    manual export."""
    q = "downloadOptions.getHasBillingMethod=false"
    if not all_states:
        q = "state=1%2C2&" + q
    url = f"{API}/v3/{GYM}/subscription/list?{q}"
    return _http(url, headers={"authorization": token, "accept": "*/*",
                               "origin": ORIGIN, "referer": f"{ORIGIN}/"})


def _save_temp(text: str, name: str) -> Path:
    p = Path(tempfile.gettempdir()) / f"beta_pull_{name}_{rbd.STAMP}.csv"
    p.write_text(text, encoding="utf-8")
    return p


def _nrows(p: Path) -> int:
    with open(p, encoding="utf-8-sig") as f:
        return max(0, sum(1 for _ in f) - 1)


def _n_active(p: Path) -> int:
    """Rows with a blank `cancelled_date` (the full list's own view of active)."""
    import csv
    with open(p, newline="", encoding="utf-8-sig") as f:
        return sum(1 for r in csv.DictReader(f)
                   if not (r.get("cancelled_date") or "").strip())


# (label, drive-id env value, canonical destination Path)
def _drive_targets():
    return [
        ("transactions", os.getenv("BETA_DRIVE_TX"), rbd.TX_DST),
        ("sessions", os.getenv("BETA_DRIVE_SES"), rbd.SES_DST),
        ("memberships", os.getenv("BETA_DRIVE_MEM"), rbd.MEM_DST),
    ]


def drive_pull_canonical() -> None:
    """Pull the current canonical CSVs from Drive into the working dir (cloud)."""
    from nudge_tool import drive_io
    print("\n=== DRIVE PULL (canonical -> working dir) ===")
    svc = drive_io._service(drive_io._RO)
    for label, fid, dst in _drive_targets():
        if not fid:
            sys.exit(f"  --drive needs BETA_DRIVE_{label[:3].upper()} (file id).")
        drive_io.pull(fid, str(dst), svc=svc)
        print(f"  pulled {label} -> {dst} ({_nrows(dst)} rows)")


def drive_push_canonical() -> None:
    """Push the merged canonical CSVs back to their Drive file ids (cloud)."""
    from nudge_tool import drive_io
    print("\n=== DRIVE PUSH (working dir -> canonical) ===")
    svc = drive_io._service(drive_io._RW)
    for label, fid, dst in _drive_targets():
        drive_io.push(str(dst), file_id=fid, svc=svc)
        print(f"  pushed {label} ({_nrows(dst)} rows) -> {fid}")


def snapshot_memberships(me: Path, drive: bool) -> None:
    """Append today's ACTIVE membership list to one long daily snapshot file.

    Why: a day-by-day record of who was active (same idea as ABC's
    member_status.csv, but kept as history): the daily active count, and a
    cancel = a membership on yesterday's list that is gone today. Since
    2026-10-15 the merge also fills `cancelled_date` in Memberships.csv from
    Beta's full list, so cancel DATES live there; this file stays the per-day
    active record. Past days cannot be rebuilt: history starts on the first
    run (2026-10-14).

    One row per membership per day, all of Beta's columns, plus `snapshot_date`
    (Eastern date) first. A re-run on the same day REPLACES that day's rows, so
    a Trigger Run never doubles a day. If Beta adds a column, it is appended to
    the header and older days show it blank."""
    import csv
    from zoneinfo import ZoneInfo
    print("\n=== MEMBER SNAPSHOT (daily active list) ===")
    fid = os.getenv("BETA_DRIVE_MEMSNAP", "").strip()
    if drive:
        if not fid:
            raise RuntimeError("BETA_DRIVE_MEMSNAP is not set (Drive file id of "
                               "Beta_Active_Members_Daily.csv)")
        from nudge_tool import drive_io
        drive_io.pull(fid, str(SNAP_DST))

    new_h, new = rbd._read(me)
    new = [r for r in new if len(r) >= len(new_h)]  # same junk-row rule as the merge
    if not new:
        raise RuntimeError("today's Beta pull has 0 memberships; not saving an "
                           "empty day (it would read as everyone cancelling)")
    day = datetime.now(ZoneInfo(SNAP_TZ)).strftime("%Y-%m-%d")

    cur_h, cur = ["snapshot_date"], []
    if SNAP_DST.exists() and SNAP_DST.stat().st_size > 0:
        with open(SNAP_DST, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        if rows:
            cur_h, cur = rows[0], rows[1:]
        if cur_h[:1] != ["snapshot_date"]:
            raise RuntimeError(f"{SNAP_DST.name} does not start with a "
                               f"snapshot_date column; not touching it")
    header = cur_h + [c for c in new_h if c not in cur_h]

    def as_dict(h, r):
        return dict(zip(h, r))

    old = [as_dict(cur_h, r) for r in cur if r]
    replaced = sum(1 for d in old if d.get("snapshot_date") == day)
    old = [d for d in old if d.get("snapshot_date") != day]
    today = [{"snapshot_date": day, **as_dict(new_h, r)} for r in new]

    # Compare with the latest earlier day: memberships (Beta `key`) and people
    # (`climber_key`). A plan switch = one membership gone + one new, same person.
    prev_day = max((d["snapshot_date"] for d in old), default=None)
    prev = [d for d in old if d["snapshot_date"] == prev_day]

    def keys(ds, col):
        return {d.get(col, "") for d in ds if d.get(col, "").strip()}

    if SNAP_DST.exists() and cur:
        rbd._backup(SNAP_DST)
    SNAP_DST.parent.mkdir(parents=True, exist_ok=True)
    with open(SNAP_DST, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header, restval="", extrasaction="ignore")
        w.writeheader()
        w.writerows(old)
        w.writerows(today)
    days = len({d["snapshot_date"] for d in old}) + 1

    print(f"  {day}: {len(today)} active memberships, "
          f"{len(keys(today, 'climber_key'))} people"
          + (f" (replaced {replaced} rows already saved for today)" if replaced else ""))
    if prev_day:
        tm, pm = keys(today, "key"), keys(prev, "key")
        tp, pp = keys(today, "climber_key"), keys(prev, "climber_key")
        print(f"  vs {prev_day}: memberships +{len(tm - pm)} new / -{len(pm - tm)} gone; "
              f"people +{len(tp - pp)} new / -{len(pp - tp)} gone")
    else:
        print("  first day in the file (nothing to compare yet)")
    added_cols = header[len(cur_h):]
    if cur and added_cols:
        print(f"  note: new Beta column(s) added to the header: {added_cols}")
    print(f"  file: {len(old) + len(today)} rows, {days} day(s)")

    if drive:
        drive_io.push(str(SNAP_DST), file_id=fid)
        print(f"  pushed snapshot -> {fid}")


def send_email(subject: str, body: str) -> None:
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587") or "587")
    user = os.getenv("SMTP_USER", "").strip()
    pw = os.getenv("SMTP_PASS", "").strip()
    to = os.getenv("NOTIFY_TO", user).strip()
    if not (user and pw and to):
        print("  email skipped (set SMTP_USER, SMTP_PASS, NOTIFY_TO to enable)")
        return
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    ctx = ssl.create_default_context()
    # Gmail intermittently answers "421 Temporary System Problem" (or drops the
    # connection). Those are transient: retry with a pause. Permanent rejections
    # (5xx, e.g. bad app password) raise immediately.
    for attempt in (1, 2, 3):
        try:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls(context=ctx)
                s.login(user, pw)
                s.send_message(msg)
            print(f"  email sent to {to}")
            return
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPResponseException,
                OSError) as e:
            code = getattr(e, "smtp_code", None)
            transient = code is None or 400 <= code < 500
            if attempt == 3 or not transient:
                raise
            wait = 45 * attempt
            print(f"  send attempt {attempt} failed ({e}); retrying in {wait}s")
            time.sleep(wait)


def run_pull(a) -> None:
    """The actual work. Prints a human report; raises on any failure."""
    now = datetime.now(timezone.utc)
    win_start = now - timedelta(days=a.days)
    tx_win_start = now - timedelta(days=max(a.tx_days, a.days))
    date_from = win_start.strftime("%Y-%m-%d")
    date_to = now.strftime("%Y-%m-%d")

    print(f"Beta pull {rbd.STAMP} | gym {GYM} | window {a.days}d "
          f"({date_from} .. {date_to} UTC); transactions re-checked over "
          f"{max(a.tx_days, a.days)}d (from {tx_win_start:%Y-%m-%d})")

    if a.drive:
        drive_pull_canonical()

    token = mint_token()
    print("\n  token minted (fresh 1-hour ID token)")

    tx = _save_temp(pull_transactions_window(token, tx_win_start, now), "transactions")
    se = _save_temp(pull_sessions(token, date_from, date_to), "sessions")
    me = _save_temp(pull_memberships(token), "memberships")
    me_all = _save_temp(pull_memberships(token, all_states=True), "memberships_all")
    print(f"  pulled: transactions={_nrows(tx)}  sessions={_nrows(se)}  "
          f"memberships={_nrows(me)} (rows in window)")
    n_all, n_all_active = _nrows(me_all), _n_active(me_all)
    print(f"  full membership list: {n_all} rows, {n_all - n_all_active} with a "
          f"cancelled_date, {n_all_active} without"
          + ("" if n_all_active == _nrows(me) else
             f"  (note: the active pull has {_nrows(me)} rows; the two lists "
             f"disagree on who is active)"))

    if a.no_merge:
        print("\n--no-merge: nothing written. Temp files:")
        print(f"  {tx}\n  {se}\n  {me}\n  {me_all}")
        return

    rbd.merge_transactions(tx)
    rbd.merge_sessions(se)
    rbd.merge_memberships(me_all)

    if a.drive:
        drive_push_canonical()
    elif a.push:
        from drive_sync import cmd_push
        folder = a.push if isinstance(a.push, str) else os.getenv("BETA_DRIVE_FOLDER")
        if not folder:
            sys.exit("\n  --push needs a folder id (arg) or BETA_DRIVE_FOLDER env.")
        print("\n=== PUSH TO DRIVE ===")
        cmd_push(folder)

    # Guarded (project rule 5): the refresh above is already done and pushed, so
    # a snapshot problem must never fail the run. It prints a greppable FAILED
    # line here AND marks the email subject, so it cannot hide in a SUCCESS body.
    global SNAP_FAILED
    try:
        snapshot_memberships(me, a.drive)
    except Exception as e:  # noqa: BLE001
        SNAP_FAILED = True
        print(f"\n  member snapshot FAILED: {type(e).__name__}: {e}\n"
              f"  (the Beta refresh above is fine; today's member list is NOT saved)\n"
              + traceback.format_exc())


SNAP_FAILED = False


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Pull Beta exports via the API and merge into canonical CSVs.")
    ap.add_argument("--days", type=int, default=2,
                    help="rolling overlap window in days for sessions (default 2)")
    ap.add_argument("--tx-days", type=int, default=90,
                    help="how far back transactions are re-pulled so later payments, "
                         "cancels and refunds on older sales land (default 90)")
    ap.add_argument("--drive", action="store_true",
                    help="cloud mode: pull canonical from Drive, merge, push back "
                         "(uses BETA_DRIVE_* file ids + BETA_*_PATH working paths)")
    ap.add_argument("--push", nargs="?", const=True, default=None,
                    metavar="FOLDER_ID",
                    help="local mode: after merging, push canonical CSVs to the "
                         "Drive folder (id, or BETA_DRIVE_FOLDER env).")
    ap.add_argument("--no-merge", action="store_true",
                    help="pull + report only; do not write canonical files")
    ap.add_argument("--notify", action="store_true",
                    help="email a success/failure report (needs SMTP_* env)")
    a = ap.parse_args()

    buf = io.StringIO()
    status = "SUCCESS"
    try:
        with contextlib.redirect_stdout(buf):
            run_pull(a)
    except BaseException:  # noqa: BLE001 - any failure must still notify
        status = "FAILURE"
        buf.write("\n\n*** RUN FAILED ***\n")
        buf.write(traceback.format_exc())

    report = buf.getvalue()
    print(report)  # echo to the real console / Render logs

    if a.notify:
        when = rbd.STAMP
        if status == "SUCCESS":
            subject = f"SUCCESS - Beta data refreshed ({when})"
            banner = ("==================================================\n"
                      "  SUCCESS - Beta data refreshed. Nothing to do.\n"
                      "==================================================\n\n")
            if SNAP_FAILED:
                subject = (f"SUCCESS - Beta data refreshed, but member snapshot "
                           f"FAILED ({when})")
                banner = ("==================================================\n"
                          "  SUCCESS - Beta data refreshed.\n"
                          "  BUT the daily member snapshot FAILED: today's\n"
                          "  active list was not saved. See 'member snapshot\n"
                          "  FAILED' below.\n"
                          "==================================================\n\n")
        else:
            subject = f"FAILURE - Beta refresh FAILED, pull manually ({when})"
            banner = ("==================================================\n"
                      "  FAILURE - the dashboard did NOT refresh today.\n"
                      "  ACTION: pull the Beta exports manually.\n"
                      "==================================================\n\n")
        try:
            send_email(subject, banner + report)
        except Exception:
            # Do NOT flip a successful run to FAILURE over a lost report email:
            # the data refreshed; exit 0 keeps the cron monitor quiet. If the
            # run itself failed, status is already FAILURE and the cron's own
            # exit-1 alert becomes the backup notification channel.
            print("  EMAIL SEND FAILED:\n" + traceback.format_exc())

    sys.exit(0 if status == "SUCCESS" else 1)


if __name__ == "__main__":
    main()
