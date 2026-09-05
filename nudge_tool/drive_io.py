"""Private Google Drive read/write for the Beta data CSVs (service-account auth).

Why: Beta has no API yet, so data still comes from a manual export + the local
merge (refresh_beta_data.py). This module is the bridge that gets those merged
CSVs OFF Luke's PC and into a private Drive folder the cloud (Render) can read,
so the live dashboard shows real CURRENT data without committing any PII to git.

Auth reuses the SAME service account as the Form-sheet reader (survey.py):
  GSHEETS_SA_KEY = path to the SA key file, OR
  GSHEETS_SA_JSON = the raw key JSON (used on the host).
The SA must be shared (Editor for push, Viewer is enough for pull) on the Drive
folder / files. Requires the Drive API enabled on the SA's GCP project.

Quota note (personal Gmail, not Workspace): a service account has no Drive
storage quota, so it generally CANNOT create brand-new files in a user's Drive
(create -> 403 storageQuotaExceeded). It CAN update the content of a file the
user already owns (the write is billed to the owner). So push() updates by
default and only attempts create as a fallback; if create fails on quota it
tells you to pre-create the file once by hand and pass its id.
"""
from __future__ import annotations

import io
import json
import os

_RW = "https://www.googleapis.com/auth/drive"
_RO = "https://www.googleapis.com/auth/drive.readonly"

# Drive throws occasional transient 5xx ("Unknown Error") on media requests;
# one blip must not abort a whole send run (it did on 2026-07-16). googleapiclient
# retries 5xx/429/connection errors with exponential backoff when num_retries is
# set, so every request in this module passes it.
_NUM_RETRIES = 5


def _load_info(text: str) -> dict:
    """Parse SA key info from text that is EITHER raw JSON OR base64-of-JSON.

    Cloud env fields / file editors mangle the key's quotes and \\n escapes, so
    base64 (plain ASCII, no escapes) is the paste-safe form. We try JSON first;
    if that fails, normalize base64 (strip whitespace, accept url-safe -/_,
    restore padding) and decode."""
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        import base64
        import re
        s = re.sub(r"\s+", "", text).replace("-", "+").replace("_", "/")
        s += "=" * (-len(s) % 4)
        return json.loads(base64.b64decode(s))


def _creds(scope: str):
    from google.oauth2 import service_account
    b64 = os.getenv("GSHEETS_SA_B64")
    if b64:
        return service_account.Credentials.from_service_account_info(
            _load_info(b64), scopes=[scope])
    key_path = os.getenv("GSHEETS_SA_KEY")
    if key_path and os.path.exists(key_path):
        with open(key_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return service_account.Credentials.from_service_account_info(
            _load_info(content), scopes=[scope])
    raw = os.getenv("GSHEETS_SA_JSON")
    if raw:
        return service_account.Credentials.from_service_account_info(
            _load_info(raw), scopes=[scope])
    raise RuntimeError("no SA creds: set GSHEETS_SA_B64 (base64 JSON), "
                       "GSHEETS_SA_KEY (file path), or GSHEETS_SA_JSON (raw JSON)")


def _service(scope: str = _RW):
    from googleapiclient.discovery import build
    return build("drive", "v3", credentials=_creds(scope), cache_discovery=False)


def find_in_folder(folder_id: str, name: str, svc=None) -> str | None:
    """Return the id of `name` inside `folder_id`, or None. Newest if duplicated."""
    svc = svc or _service(_RO)
    safe = name.replace("'", "\\'")
    q = f"'{folder_id}' in parents and name = '{safe}' and trashed = false"
    res = svc.files().list(q=q, fields="files(id,name,modifiedTime)",
                           orderBy="modifiedTime desc",
                           pageSize=10).execute(num_retries=_NUM_RETRIES)
    files = res.get("files", [])
    return files[0]["id"] if files else None


def push(local_path: str, *, folder_id: str | None = None, file_id: str | None = None,
         name: str | None = None, svc=None) -> str:
    """Upload local_path's content to Drive. Returns the Drive file id.

    Resolution order: explicit file_id -> find name in folder_id -> create in
    folder_id. Updates content in place when the file exists (owner-billed, so
    it works for a personal-Gmail SA)."""
    from googleapiclient.http import MediaFileUpload
    svc = svc or _service(_RW)
    name = name or os.path.basename(local_path)
    if file_id is None and folder_id:
        file_id = find_in_folder(folder_id, name, svc)
    media = MediaFileUpload(local_path, mimetype="text/csv", resumable=True)
    if file_id:
        svc.files().update(fileId=file_id,
                           media_body=media).execute(num_retries=_NUM_RETRIES)
        return file_id
    if not folder_id:
        raise RuntimeError("push needs folder_id or file_id")
    try:
        f = svc.files().create(body={"name": name, "parents": [folder_id]},
                               media_body=media,
                               fields="id").execute(num_retries=_NUM_RETRIES)
        return f["id"]
    except Exception as e:
        if "storageQuota" in str(e) or "quota" in str(e).lower():
            raise RuntimeError(
                f"SA can't create '{name}' (no Drive quota on a personal-Gmail SA). "
                f"Create the file once by hand in the folder, share it with the SA "
                f"as Editor, then pushes will UPDATE it.") from e
        raise


def pull(file_id: str, local_path: str, svc=None) -> str:
    """Download a Drive file's content to local_path. Returns local_path."""
    from googleapiclient.http import MediaIoBaseDownload
    svc = svc or _service(_RO)
    buf = io.BytesIO()
    req = svc.files().get_media(fileId=file_id)
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk(num_retries=_NUM_RETRIES)
    data = buf.getvalue()
    os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
    with open(local_path, "wb") as f:
        f.write(data)
    return local_path
