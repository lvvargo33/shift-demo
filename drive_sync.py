"""Sync the canonical Beta CSVs to/from a private Google Drive folder.

The manual cadence becomes:
    1. export the incremental window from Beta (still manual; no API yet)
    2. py refresh_beta_data.py --transactions ... --sessions ... --memberships ...
    3. py drive_sync.py push --folder <FOLDER_ID>
Then the cloud (Render) pulls the same files via the SA, so /live shows real
current data with no PII in git and no PC needed at demo time.

Subcommands:
    probe  --folder <id>   one-time capability check: can the SA create/update
                           files in this folder? (run after enabling Drive API)
    push   --folder <id>   upload the 3 canonical CSVs into the folder. Writes
                           drive_manifest.json (name -> file id) for pull/Render.
    pull  [--folder <id>]  download the 3 files back to their canonical local
                           paths. Uses drive_manifest.json or BETA_DRIVE_* env.

Auth: set GSHEETS_SA_KEY (key file path) or GSHEETS_SA_JSON (raw JSON), same SA
as the Form reader. The SA must be shared on the folder (Editor to push).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from nudge_tool import drive_io

BASE = Path(__file__).resolve().parent
SHIFT = BASE.parent
MANIFEST = BASE / "drive_manifest.json"

# logical name -> (canonical local path, Drive file name)
FILES = {
    "transactions": (SHIFT / "SHIFT_Docs" / "Transactions.csv", "Transactions.csv"),
    "memberships":  (SHIFT / "SHIFT_Docs" / "Memberships.csv",  "Memberships.csv"),
    "sessions":     (BASE / "sessions_Shift Climbing  (2).csv", "Sessions.csv"),
}


def _load_manifest() -> dict:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {}


def cmd_probe(folder_id: str) -> None:
    from googleapiclient.http import MediaInMemoryUpload
    svc = drive_io._service()
    print(f"SA can see folder {folder_id} ...")
    try:
        meta = svc.files().get(fileId=folder_id, fields="id,name").execute()
        print(f"  ok: '{meta['name']}'")
    except Exception as e:
        sys.exit(f"  CANNOT access folder (share it with the SA as Editor?): {repr(e)[:200]}")
    # try create
    fid = None
    try:
        media = MediaInMemoryUpload(b"probe,ok\n1,2\n", mimetype="text/csv")
        f = svc.files().create(body={"name": "_probe.csv", "parents": [folder_id]},
                               media_body=media, fields="id").execute()
        fid = f["id"]
        print("  CREATE works -> push can make files itself (no manual pre-create).")
    except Exception as e:
        if "storageQuota" in str(e) or "quota" in str(e).lower():
            print("  CREATE blocked (SA has no Drive quota, expected on personal Gmail).")
            print("  => pre-create the 3 files by hand once; push will UPDATE them.")
        else:
            print(f"  CREATE failed: {repr(e)[:200]}")
    if fid:
        try:
            svc.files().delete(fileId=fid).execute()
            print("  cleaned up probe file.")
        except Exception:
            pass


def cmd_push(folder_id: str) -> None:
    svc = drive_io._service()
    manifest = _load_manifest()
    manifest["_folder"] = folder_id
    for key, (local, name) in FILES.items():
        if not local.exists():
            print(f"  SKIP {key}: missing local file {local}")
            continue
        fid = manifest.get(key)  # reuse known id if we have one
        fid = drive_io.push(str(local), folder_id=folder_id, file_id=fid, name=name, svc=svc)
        manifest[key] = fid
        size = local.stat().st_size
        print(f"  pushed {key:12} {name:18} {size:>10,} bytes  -> {fid}")
    MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"  manifest -> {MANIFEST.name}")
    print("\n  Render env (read path): set these so the host pulls real data:")
    print(f"    BETA_DRIVE_TX={manifest.get('transactions','')}")
    print(f"    BETA_DRIVE_SES={manifest.get('sessions','')}")
    print(f"    BETA_DRIVE_MEM={manifest.get('memberships','')}")


def _resolve_ids(folder_id: str | None) -> dict:
    """File ids from (1) env BETA_DRIVE_*, (2) manifest, (3) listing the folder."""
    env = {"transactions": os.getenv("BETA_DRIVE_TX"),
           "sessions": os.getenv("BETA_DRIVE_SES"),
           "memberships": os.getenv("BETA_DRIVE_MEM")}
    if all(env.values()):
        return env
    manifest = _load_manifest()
    ids = {k: manifest.get(k) for k in FILES}
    if all(ids.values()):
        return ids
    if folder_id:
        svc = drive_io._service(drive_io._RO)
        return {k: drive_io.find_in_folder(folder_id, name, svc) for k, (_, name) in FILES.items()}
    sys.exit("pull needs BETA_DRIVE_* env, a drive_manifest.json, or --folder")


def cmd_pull(folder_id: str | None) -> None:
    ids = _resolve_ids(folder_id)
    svc = drive_io._service(drive_io._RO)
    for key, (local, name) in FILES.items():
        fid = ids.get(key)
        if not fid:
            print(f"  SKIP {key}: no file id")
            continue
        drive_io.pull(fid, str(local), svc=svc)
        print(f"  pulled {key:12} {name:18} -> {local.name} ({local.stat().st_size:,} bytes)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Sync Beta CSVs to/from a private Drive folder.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("probe", "push", "pull"):
        p = sub.add_parser(c)
        p.add_argument("--folder", default=os.getenv("BETA_DRIVE_FOLDER"),
                       help="Drive folder id (or set BETA_DRIVE_FOLDER)")
    a = ap.parse_args()
    if a.cmd in ("probe", "push") and not a.folder:
        ap.error(f"{a.cmd} needs --folder <id> (or BETA_DRIVE_FOLDER)")
    if a.cmd == "probe":
        cmd_probe(a.folder)
    elif a.cmd == "push":
        cmd_push(a.folder)
    elif a.cmd == "pull":
        cmd_pull(a.folder)


if __name__ == "__main__":
    main()
