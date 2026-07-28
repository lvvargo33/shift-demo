"""Push SHIFT's per-automation + per-variant stats to the shared
"Send It - All Gyms" Google Sheet (tabs: SHIFT, SHIFT Variants).

Read-only on gym data; writes only the two SHIFT tabs. Reuses the tool's own
open-counting rules (campaign-pinned opens via the sent-event campaign id) so
every number matches the live dashboard's logic, not Mailchimp's screens.

Run locally:  py allgyms_push.py            (needs GSHEETS_SA_KEY/_B64/_JSON +
                                             Mailchimp creds from .env)
Cron:         called at the end of send_nudges.py, guarded so a stats failure
              can never fail the send run.

Measurement rules baked in (2026-07-28):
- outreach log rows with mode=test are ignored.
- opens/clicks come from each recipient's activity feed, opens pinned to the
  exact journey email that was sent (engine._sent_campaign_ids).
- Q1 taps (embed test) count only when the tapping email actually HAS an
  embed-tagged send on/before the tap date, and only the FIRST tap per email
  counts. This filters Mailchimp's activation-time link checker and any
  security scanners that crawl all five buttons at once.
- returned-after-send = any visit day strictly after that email's sent date;
  converted-after-send = membership_created on/after sent date. Both need the
  local Beta CSVs (fresh daily via beta_pull on the cron host).
- rows with sends < 30 carry a small-n flag: directional only.
"""
from __future__ import annotations

import csv
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from nudge_tool import config, drive_io, engine, ingest, survey
from nudge_tool.mailchimp_client import MailchimpClient

ALLGYMS_SHEET_ID = os.getenv(
    "ALLGYMS_SHEET_ID", "1s4Cg7vZbriq1PjDGLPkc8PZUr2ou-XKK3Hr3I0QFaHo")
OUTREACH_DRIVE_ID = os.getenv(
    "OUTREACH_DRIVE_ID", "1SUUYeh_7DabmIl6Ae7bSFLxsjGBUbdbi")
SMALL_N = 30

# trigger_name -> display group (falls back to the raw name)
FUNNEL_LABELS = {
    "survey_request": "FTV survey (day 1-2)",
    "first_visit_reengage": "Reengage 50% offer (day 5-7)",
    "nudge_round_two": "Round two (high intent)",
    "nudge_pricing": "Blocker: pricing",
    "nudge_crowding": "Blocker: crowding",
    "nudge_too_hard": "Blocker: too hard",
    "nudge_intimidating": "Blocker: intimidating",
    "nudge_frontdesk": "Blocker: front desk",
    "nudge_confusing": "Blocker: confusing",
    "nudge_routes": "Blocker: routes",
    "comeback": "Trial win-back",
}
EMBED_TAGS = {"FTV_survey_subject_a_embed", "FTV_survey_subject_b_embed"}


def _sheets_service():
    from googleapiclient.discovery import build
    creds = drive_io._creds("https://www.googleapis.com/auth/spreadsheets")
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _pull_outreach(dest: Path) -> list[dict]:
    try:
        drive_io.pull(OUTREACH_DRIVE_ID, str(dest))
    except Exception as exc:  # stale copy beats no stats
        print(f"  allgyms: outreach pull failed ({exc}); using existing copy")
    if not dest.exists():
        return []
    with open(dest, encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f)
                if (r.get("mode") or "").strip() != "test"]


def _pct(a: int, b: int) -> str:
    return f"{100 * a / b:.0f}%" if b else ""


def collect(client) -> tuple[list[list], list[list]]:
    """Build the row grids for the SHIFT and SHIFT Variants tabs."""
    rows = _pull_outreach(BASE / "_allgyms_outreach_snapshot.csv")
    ds = ingest.load(client)
    mc = MailchimpClient(config.load_settings(client, require=True))

    # earliest real send per (email, trigger, tag)
    sends: list[dict] = []
    for r in rows:
        email = (r.get("email") or "").strip().lower()
        sent = (r.get("sent_date") or "").strip()[:10]
        trig = (r.get("trigger_name") or r.get("trigger") or "").strip()
        tag = (r.get("tag") or "").strip()
        if email and sent and tag:
            sends.append({"email": email, "sent": sent, "trig": trig, "tag": tag})

    emails = sorted({s["email"] for s in sends})
    feeds = {e: mc.member_activity(e) for e in emails}
    cids = {a.get("campaign_id") for ev in feeds.values()
            for a in ev or [] if a.get("campaign_id")}
    journey_ids = {cid for cid in cids if mc.campaign_type(cid) == "automation-email"}

    # survey responses by email (earliest)
    resp_by_email: dict[str, str] = {}
    try:
        for r in survey.load_responses(client):
            e = (r.email or "").strip().lower()
            d = (r.answered_at or "")[:10]
            if e and (e not in resp_by_email or d < resp_by_email[e]):
                resp_by_email[e] = d
    except Exception as exc:
        print(f"  allgyms: survey read failed ({exc}); responses omitted")

    # Q1 taps (embed arms), first valid tap per email only
    taps_valid: dict[str, str] = {}
    try:
        svc = _sheets_service()
        got = svc.spreadsheets().values().get(
            spreadsheetId=client.survey.get("gsheet_id"),
            range="Taps!A:D").execute().get("values", [])
        embed_sent = {s["email"]: s["sent"] for s in sends if s["tag"] in EMBED_TAGS}
        for r in got[1:]:
            if len(r) < 4:
                continue
            ts, email, q, tag = r[0][:10], r[1].strip().lower(), r[2], r[3]
            if email in embed_sent and ts >= embed_sent[email] \
                    and email not in taps_valid:
                taps_valid[email] = q
    except Exception as exc:
        print(f"  allgyms: taps read failed ({exc}); taps omitted")

    # per-send outcome flags
    by_trig: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    by_tag: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    trig_of_tag: dict[str, str] = {}
    for s in sends:
        email, sent = s["email"], s["sent"]
        ev = feeds.get(email) or []
        sent_ids = engine._sent_campaign_ids(ev, sent, journey_ids)
        delivered = bool(sent_ids)
        opened = delivered and engine._has_event(ev, "open", sent, campaign_ids=sent_ids)
        clicked = engine._has_event(ev, "click", sent, engine._SURVEY_LINK_MARKERS)
        c = ds.climbers_by_email.get(email) if hasattr(ds, "climbers_by_email") else None
        if c is None:
            c = next((x for x in ds.climbers.values()
                      if (x.email or "").strip().lower() == email), None)
        returned = bool(c and any(v > sent for v in c.visit_days))
        converted = bool(c and getattr(c, "membership_created", "")
                         and c.membership_created >= sent)
        responded = email in resp_by_email and resp_by_email[email] >= sent
        tapped = s["tag"] in EMBED_TAGS and email in taps_valid
        for bucket, key in ((by_trig, s["trig"] or s["tag"]), (by_tag, s["tag"])):
            b = bucket[key]
            b["sends"] += 1
            b["delivered"] += delivered
            b["opens"] += opened
            b["clicks"] += clicked
            b["returned"] += returned
            b["converted"] += converted
            b["responses"] += responded
            b["taps"] += tapped
        trig_of_tag[s["tag"]] = s["trig"] or s["tag"]

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header = ["Automation", "Sends", "Delivered", "Opens", "Open %",
              "Survey clicks", "Q1 taps", "Responses", "Response %",
              "Returned after send", "Return %", "Converted after send",
              "Conversion %", "Note"]

    def rows_for(bucket: dict, label_of) -> list[list]:
        out = []
        for key in sorted(bucket, key=lambda k: -bucket[k]["sends"]):
            b = bucket[key]
            note = f"small sample (under {SMALL_N}), directional only" \
                if b["sends"] < SMALL_N else ""
            out.append([
                label_of(key), b["sends"], b["delivered"], b["opens"],
                _pct(b["opens"], b["delivered"]), b["clicks"], b["taps"],
                b["responses"], _pct(b["responses"], b["sends"]),
                b["returned"], _pct(b["returned"], b["sends"]),
                b["converted"], _pct(b["converted"], b["sends"]), note,
            ])
        return out

    shift_grid = (
        [[f"SHIFT (Send It) - updated {stamp} - opens pinned to the exact "
          f"email, Send It counting rules (Mailchimp screens include old test "
          f"contacts and will not match)"],
         header]
        + rows_for(by_trig, lambda k: FUNNEL_LABELS.get(k, k)))
    var_grid = (
        [[f"SHIFT variants (every A/B arm) - updated {stamp} - a row per "
          f"exact tag; subject test = compare _a vs _b, embed test = compare "
          f"_embed vs not"],
         ["Tag"] + header[1:]]
        + rows_for(by_tag, lambda k: k))
    return shift_grid, var_grid


def push(slug: str = "shift") -> None:
    client = config.load_client(slug)
    shift_grid, var_grid = collect(client)
    svc = _sheets_service()
    meta = svc.spreadsheets().get(spreadsheetId=ALLGYMS_SHEET_ID).execute()
    have = {s["properties"]["title"] for s in meta["sheets"]}
    reqs = [{"addSheet": {"properties": {"title": t}}}
            for t in ("SHIFT", "SHIFT Variants") if t not in have]
    if reqs:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=ALLGYMS_SHEET_ID, body={"requests": reqs}).execute()
    for tab, grid in (("SHIFT", shift_grid), ("SHIFT Variants", var_grid)):
        svc.spreadsheets().values().clear(
            spreadsheetId=ALLGYMS_SHEET_ID, range=f"'{tab}'!A:Z").execute()
        svc.spreadsheets().values().update(
            spreadsheetId=ALLGYMS_SHEET_ID, range=f"'{tab}'!A1",
            valueInputOption="RAW", body={"values": grid}).execute()
        print(f"  allgyms: wrote {len(grid) - 2} rows to '{tab}'")


if __name__ == "__main__":
    push("shift")
