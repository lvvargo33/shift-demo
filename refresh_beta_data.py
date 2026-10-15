"""Merge incremental Beta exports into the canonical data store (append + dedup).

Beta has no API, so the cadence is: export an incremental window from Beta
(transactions + sessions are append-logs; subscriptions is a member snapshot),
then run this to fold the new rows into the canonical CSVs the tool reads.

Dedup keys:
  transactions  -> column `id` (unique per transaction; NEW wins on collision,
                   so a later-finalized/refunded state replaces the older row)
  sessions      -> full-row tuple (no id column; identical check-in rows are dups)
  memberships   -> subscription `key` (col 0). UNION: keep all current rows,
                   add new-file rows whose key is unseen. This preserves the
                   "ever a member => converted" semantics even when Beta's export
                   is active-only (drops cancelled members the engine still needs
                   to treat as converted). Since 2026-10-15 the daily pull hands
                   this Beta's FULL list, and the one column copied onto a row
                   already on file is `cancelled_date` (see merge_memberships).

Backs up each canonical file (timestamped) before writing. Prints a sanity
report (rows before/after, $ total for transactions, new max date, samples).

Usage:
  py refresh_beta_data.py \
      --transactions "C:/.../transactions_...csv" \
      --sessions     "C:/.../sessions_... (3).csv" \
      --memberships  "C:/.../subscriptions...csv"
Any of the three may be omitted to skip that file.
"""
from __future__ import annotations

import argparse
import csv
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
SHIFT = BASE.parent  # ...\Redesign\SHIFT

# Canonical destinations the `shift` client.json points at. Env overrides let the
# cloud cron (which has no persistent disk) point these at its working dir; on
# Luke's PC the defaults below are used.
TX_DST = Path(os.getenv("BETA_TX_PATH") or (SHIFT / "SHIFT_Docs" / "Transactions.csv"))
MEM_DST = Path(os.getenv("BETA_MEM_PATH") or (SHIFT / "SHIFT_Docs" / "Memberships.csv"))
SES_DST = Path(os.getenv("BETA_SES_PATH") or (BASE / "sessions_Shift Climbing  (2).csv"))

STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")


def _read(path: Path) -> tuple[list[str], list[list[str]]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    return rows[0], rows[1:]


def _write(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def _backup(path: Path) -> None:
    bak = path.with_name(f"{path.stem}_backup_{STAMP}{path.suffix}")
    shutil.copyfile(path, bak)
    print(f"  backup -> {bak.name}")


def _datecol(rows: list[list[str]], idx: int) -> str:
    ds = sorted((r[idx][:10] for r in rows if len(r) > idx and r[idx].strip()))
    return ds[-1] if ds else "NA"


def merge_transactions(new_path: Path) -> None:
    print("\n=== TRANSACTIONS ===")
    cur_h, cur = _read(TX_DST)
    new_h, new = _read(new_path)
    if new_h != cur_h:
        sys.exit("  ABORT: transactions header mismatch; not the same export shape.")
    id_i = cur_h.index("id")
    amt_i = cur_h.index("total_amount")
    time_i = cur_h.index("time")

    def total(rows):
        s = 0.0
        for r in rows:
            try:
                s += float(r[amt_i])
            except (ValueError, IndexError):
                pass
        return s

    before_n, before_amt = len(cur), total(cur)
    merged = {r[id_i]: r for r in cur}      # current first
    added = 0
    for r in new:                            # NEW wins on collision
        k = r[id_i]
        if k not in merged:
            added += 1
        merged[k] = r
    out = list(merged.values())
    out.sort(key=lambda r: r[time_i])
    _backup(TX_DST)
    _write(TX_DST, cur_h, out)
    print(f"  rows: {before_n} -> {len(out)}  (+{added} new ids, "
          f"{len(new) - added} overlapping ids refreshed)")
    print(f"  $total_amount: {before_amt:,.2f} -> {total(out):,.2f}")
    print(f"  max time: {_datecol(cur, time_i)} -> {_datecol(out, time_i)}")
    print(f"  sample new rows (id | time | climber | amount):")
    for r in out[-3:]:
        print(f"    {r[id_i]} | {r[time_i][:10]} | {r[cur_h.index('climber_name')]} | {r[amt_i]}")


def merge_sessions(new_path: Path) -> None:
    print("\n=== SESSIONS ===")
    cur_h, cur = _read(SES_DST)
    new_h, new = _read(new_path)
    if new_h != cur_h:
        sys.exit("  ABORT: sessions header mismatch.")
    ck_i = cur_h.index("climber_key")
    entry_i = cur_h.index("entry")
    exit_i = cur_h.index("exit")

    # Dedup by (climber_key, entry): a single check-in is exported twice across
    # an overlapping window -- once before the nightly checkout (blank exit) and
    # once after (exit + session_duration filled). Full-row matching let both
    # through (double-counted visit). Key on the immutable check-in instead and
    # keep the COMPLETED row (with an exit) regardless of which arrived last.
    def key(r):
        return (r[ck_i], r[entry_i])

    def keep(existing, cand):
        ex_e = existing[exit_i].strip() if len(existing) > exit_i else ""
        ex_c = cand[exit_i].strip() if len(cand) > exit_i else ""
        if ex_c and not ex_e:
            return cand          # candidate is the checked-out version
        if ex_e and not ex_c:
            return existing      # existing already checked out
        return cand              # same completeness -> newer wins

    before_n = len(cur)
    merged: dict = {}
    for r in cur:
        k = key(r)
        merged[k] = keep(merged[k], r) if k in merged else r
    collapsed = before_n - len(merged)   # dup pairs already sitting in the file
    added = 0
    for r in new:
        k = key(r)
        if k in merged:
            merged[k] = keep(merged[k], r)
        else:
            merged[k] = r
            added += 1
    out = list(merged.values())
    out.sort(key=lambda r: r[entry_i])
    _backup(SES_DST)
    _write(SES_DST, cur_h, out)
    print(f"  rows: {before_n} -> {len(out)}  (+{added} new check-ins, "
          f"{len(new) - added} overlapping refreshed, "
          f"{collapsed} pre-existing dup pair(s) collapsed)")
    print(f"  dedup key: (climber_key, entry); completed row wins")
    print(f"  max entry: {_datecol(cur, entry_i)} -> {_datecol(out, entry_i)}")


def merge_memberships(new_path: Path) -> None:
    """Union on subscription `key`, plus ONE update on rows already on file:
    `cancelled_date`.

    Since 2026-10-15 the pull hands this Beta's FULL list (active and
    cancelled), so a membership saved while active comes back with its
    `cancelled_date` filled once it ends; that value is copied onto the saved
    row. Nothing else on a saved row changes (the engine reads climber_key,
    prices, metadata, email and created_date, and those stay exactly as first
    seen), and a blank incoming date never blanks a saved one (counted and
    printed instead). Unseen keys are added whole, cancelled or not."""
    print("\n=== MEMBERSHIPS ===")
    cur_h, cur = _read(MEM_DST)
    new_h, new = _read(new_path)
    key_i = cur_h.index("key")
    ck_i = cur_h.index("climber_key")
    cd_i = cur_h.index("cancelled_date") if "cancelled_date" in cur_h else None
    by_key = {r[key_i]: r for r in cur}      # rows shared with `out` (mutated in place)
    cur_climbers = {r[ck_i] for r in cur if len(r) > ck_i and r[ck_i].strip()}
    before_n = len(cur)
    out = list(cur)
    added = added_cancelled = 0
    new_climbers = set()
    # map new-file columns onto the current header by name (new lacks billing_issues)
    nidx = {name: i for i, name in enumerate(new_h)}
    ncd = nidx.get("cancelled_date")
    skipped = 0
    cd_filled = cd_changed = cd_kept = 0

    def n_cancelled(rows):
        if cd_i is None:
            return 0
        return sum(1 for r in rows if len(r) > cd_i and r[cd_i].strip())

    before_cd = n_cancelled(cur)
    for r in new:
        if len(r) < len(new_h):   # incomplete/junk row (e.g. a stray '{}' trailer)
            skipped += 1
            continue
        k = r[nidx["key"]]
        if k in by_key:
            if cd_i is not None and ncd is not None:
                row = by_key[k]
                incoming = r[ncd].strip()
                saved = row[cd_i].strip() if len(row) > cd_i else ""
                if incoming and incoming != saved:
                    if len(row) <= cd_i:
                        row.extend([""] * (cd_i + 1 - len(row)))
                    row[cd_i] = incoming
                    if saved:
                        cd_changed += 1
                    else:
                        cd_filled += 1
                elif saved and not incoming:
                    cd_kept += 1
            continue
        row = [r[nidx[name]] if name in nidx and nidx[name] < len(r) else "" for name in cur_h]
        by_key[k] = row
        out.append(row)
        added += 1
        if cd_i is not None and row[cd_i].strip():
            added_cancelled += 1
        ck = row[ck_i].strip()
        if ck and ck not in cur_climbers:
            new_climbers.add(ck)
    if skipped:
        print(f"  note: skipped {skipped} short/blank row(s) in the new file")
    _backup(MEM_DST)
    _write(MEM_DST, cur_h, out)
    print(f"  rows: {before_n} -> {len(out)}  (+{added} new subscription rows, "
          f"{added_cancelled} of them already cancelled)")
    print(f"  brand-new member climber_keys added: {len(new_climbers)}")
    if cd_i is None or ncd is None:
        print("  cancelled_date: column missing from the "
              + ("canonical file" if cd_i is None else "new file") + "; nothing updated")
    else:
        print(f"  cancelled_date: filled on {cd_filled} saved row(s), changed on "
              f"{cd_changed}, {cd_kept} saved date(s) kept where the pull sent blank")
        print(f"  rows with a cancelled_date: {before_cd} -> {n_cancelled(out)} of {len(out)}")
    print(f"  (union keeps cancelled/historical members so 'ever converted' holds)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Merge incremental Beta exports into canonical CSVs.")
    ap.add_argument("--transactions")
    ap.add_argument("--sessions")
    ap.add_argument("--memberships")
    ap.add_argument("--push", nargs="?", const=True, default=None,
                    metavar="FOLDER_ID",
                    help="after merging, push the canonical CSVs to the Drive "
                         "folder (id, or BETA_DRIVE_FOLDER env). Use alone to "
                         "push current files without merging. Needs GSHEETS_SA_KEY.")
    a = ap.parse_args()
    if not any([a.transactions, a.sessions, a.memberships, a.push]):
        ap.error("give at least one of --transactions / --sessions / --memberships / --push")
    print(f"Refresh run {STAMP}")
    if a.transactions:
        merge_transactions(Path(a.transactions))
    if a.sessions:
        merge_sessions(Path(a.sessions))
    if a.memberships:
        merge_memberships(Path(a.memberships))
    if a.push:
        import os
        from drive_sync import cmd_push
        folder = a.push if isinstance(a.push, str) else os.getenv("BETA_DRIVE_FOLDER")
        if not folder:
            sys.exit("\n  --push needs a folder id (arg) or BETA_DRIVE_FOLDER env.")
        print("\n=== PUSH TO DRIVE ===")
        cmd_push(folder)
    print("\nDone. Re-run: py -m nudge_tool run --client shift --skip-status")


if __name__ == "__main__":
    main()
