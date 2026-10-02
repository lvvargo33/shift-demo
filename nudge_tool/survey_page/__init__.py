"""Chris's native survey PAGE, mounted inside the live dashboard
(ROADMAP_SPEC_2026-09.md Block 18, built 2026-10-02; Luke's call: no new
Render service, the page answers on the dashboard's own host).

What lives here
  native_survey.py, native_survey_definitions.py, native_survey_http.py,
  native_survey_production.py, native_survey_service.py, native_survey_store.py,
  native_survey_page.html / .js / .css
      Chris's files, copied BYTE FOR BYTE from patch 0001 of
      shift-native-survey-handoff.zip (2026-09-20). Never edit them here; the
      sha256 of each is pinned in CHRIS_FILES and checked by
      _verify_survey_page.py so drift shows up. `native_survey.py` is HIS
      module (ids, hashes, transitions); OUR island module of the same name
      lives one level up (nudge_tool/native_survey.py) and is untouched.
  mount.py
      Our adapter: a request-handler base class that serves his five routes
      (/survey, /survey.js, /survey.css, /survey/state, /survey/answer) from
      the dashboard process on the dashboard's one Firestore client. Imported
      ONLY when this service has the Firestore env (handler_base()), so a
      dashboard without it (ABC today) never loads google.cloud.firestore.

Switches (the page serves only when ALL are true; otherwise every page path
answers 503 {"status":"disabled"}, the shape of his own dark launcher):
  client.json survey.enabled + survey.native.enabled   the island's read switch
  FIRESTORE_PROJECT_ID + a key file                     Firestore reachable
  NATIVE_SURVEY_PUBLIC_BASE_URL                         this dashboard's own
                                                        origin, e.g.
                                                        https://shift-live-dash.onrender.com
                                                        (exactly https://<host>,
                                                        his config rejects
                                                        anything else)
  NATIVE_SURVEY_SUPPORT_EMAIL                           shown on the page; falls
                                                        back to client.links
                                                        contact_email

So the flag flip in client.json is the one go-live switch for both halves:
/s starts minting AND the page starts answering on the same deploy.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler

PAGE_PATHS = frozenset({"/survey", "/survey.js", "/survey.css",
                        "/survey/state", "/survey/answer"})

# sha256 of Chris's files as extracted from the bundle on 2026-10-02.
CHRIS_FILES = {
    "native_survey.py": "b2ebc601146dd3872beaefe3ae665d6af93f968e3684eb8f5bbfbba5bbc28045",
    "native_survey_definitions.py": "cf3985957b70f8629756b1b1d62de8ad7619baac34414717e1029b1191de4a53",
    "native_survey_http.py": "89890f08a8e83311492afedaf7361dff5e7592e4c25122d8ef5cf417c0bd5938",
    "native_survey_production.py": "409a13ab03903176ccae64f74f8cc23ea29f6cde8401518dc33a2b8eac0f3b3e",
    "native_survey_service.py": "ce8a82f4044c0aab27f0852b33c71adceff16e510b988973eb87bfaeb08375f6",
    "native_survey_store.py": "f20a3339e06f8286c3694b26a2bd8e55775603dd45bcbaa6621d038a50f40c1a",
    "native_survey_page.html": "ed8de56a1b41acb495bfdf05f82851787b270d212e92e44201812dc95c497f9d",
    "native_survey_page.js": "a413407f9c739ec423e315924b0e2d95bd490217a98c398aab28415db19aeea6",
    "native_survey_page.css": "c1b2c80144c1dd56f9b1d022d5b5c2717436b502561de635c0a250d67ccd6b96",
}

_DISABLED = b'{"status":"disabled"}'


def is_page_path(path: str) -> bool:
    return path in PAGE_PATHS


def env_ready() -> bool:
    """This service can reach Firestore at all (project id + a key file). The
    same test the island uses for minting, so the two halves load together."""
    from .. import native_survey
    return native_survey.firestore_ready()


class DisabledPageHandler(BaseHTTPRequestHandler):
    """Handler base for a dashboard WITHOUT the Firestore env: the page paths
    answer 503 'disabled'. Stdlib only; nothing from google.cloud is imported."""

    def page_get(self) -> None:
        self._page_disabled()

    def page_post(self) -> None:
        self._page_disabled()

    def _page_disabled(self) -> None:
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(_DISABLED)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(_DISABLED)
        except (BrokenPipeError, ConnectionResetError):
            pass


def handler_base() -> type:
    """The class the dashboard's Handler inherits from. With the Firestore env
    present this is the real mount (imports Chris's modules + google.cloud);
    without it, the stdlib stub above."""
    if env_ready():
        try:
            from .mount import MountedHandler
            return MountedHandler
        except ImportError as exc:  # the firestore library missing: dashboard still starts
            print(f"  survey page: NOT mounted, import failed ({exc}); page paths answer 503")
    return DisabledPageHandler
