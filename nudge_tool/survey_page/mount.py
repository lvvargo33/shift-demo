"""The adapter between the dashboard's request handler and Chris's survey page
(ROADMAP Block 18). Imported only via survey_page.handler_base() when the
Firestore env is present.

How it fits together
  live_server.Handler inherits MountedHandler, which inherits his
  ProductionHandler -> SurveyHandler -> BaseHTTPRequestHandler. The dashboard's
  own do_GET / do_POST stay in charge; for the five page paths they call
  page_get() / page_post() here, which (a) make sure the page is built for
  this service (config validated by HIS ProductionConfig, repository on the
  dashboard's one Firestore client) and (b) hand the request to his handler
  code unchanged. His checks still apply on every request: Host must equal
  the configured public host, X-Forwarded-Proto must be https (Render's proxy
  sets both), POSTs must carry the matching Origin and one Authorization
  header. Nothing here reads a token or logs a URL.

Why one Firestore client: his ProductionRepository builds its own
firestore.Client; MountedRepository below binds the same collections to the
client the island already holds (native_survey._db()), so the process keeps
one client (~12 MB) instead of two.
"""
from __future__ import annotations

import os
import threading
import time

from .. import native_survey as island
from ..config import load_client
from .native_survey import GymContext, SurveyError
from .native_survey_production import ProductionConfig, ProductionRepository
from .native_survey_service import ProductionHandler

_NEGATIVE_CACHE_SECONDS = 60.0


def support_email(client) -> str:
    """Shown on the page ("contact Send It support"): the env var, else the
    gym's own contact address from client.json. Never Chris's."""
    env = (os.getenv("NATIVE_SURVEY_SUPPORT_EMAIL") or "").strip()
    if env:
        return env
    links = getattr(client, "links", None) or {}
    return (links.get("contact_email") or "").strip()


def page_enabled(client) -> bool:
    """Everything the page needs: the island's mint switches (client flag +
    Firestore env + public base URL) plus a support address."""
    return island.mint_enabled(client) and bool(support_email(client))


class MountedRepository(ProductionRepository):
    """His production repository bound to an existing Firestore client."""

    def __init__(self, config: ProductionConfig, *, db):  # noqa: D107
        if not isinstance(config, ProductionConfig):
            raise SurveyError("production configuration required")
        if db is None:
            raise SurveyError("firestore client required")
        self._context = GymContext(config.gym_id)
        self._db = db
        self._root = db.collection("gyms").document(config.gym_id)


def build(client, *, db=None) -> tuple[ProductionConfig, MountedRepository]:
    """Validate the env through HIS ProductionConfig (raises SurveyError with
    a plain reason on anything off) and bind the repository."""
    config = ProductionConfig(
        island.gym_id(client),
        os.getenv("FIRESTORE_PROJECT_ID", "").strip(),
        os.getenv("FIRESTORE_DATABASE_ID", "").strip() or "(default)",
        island.public_base_url(),
        support_email(client),
    )
    return config, MountedRepository(config, db=db if db is not None else island._db())


class MountedHandler(ProductionHandler):
    """Base class for the dashboard Handler. Adds page_get / page_post; keeps
    his do_GET / do_POST untouched (reached through super())."""

    _page_lock = threading.Lock()

    def _page_ready(self) -> bool:
        srv = self.server
        if getattr(srv, "repository", None) is not None:
            return True
        with MountedHandler._page_lock:
            if getattr(srv, "repository", None) is not None:
                return True
            until = getattr(srv, "_page_retry_at", 0.0)
            if time.monotonic() < until:
                return False
            try:
                client = load_client(srv.default_client)
                if not page_enabled(client):
                    reason = "switched off (client flag, Firestore env, public base URL or support email missing)"
                    raise SurveyError(reason)
                config, repository = build(client)
                # repository is the lock-free readiness flag: assign it LAST
                srv.config = config
                srv.support_email = config.support_email
                srv.repository = repository
                print(f"  survey page: mounted for {config.gym_id} at {config.public_origin}")
                return True
            except Exception as exc:  # noqa: BLE001 (never take the dashboard down)
                msg = f"  survey page: not mounted ({type(exc).__name__}: {exc})"
                if msg != getattr(srv, "_page_last_msg", ""):
                    print(msg)
                    srv._page_last_msg = msg
                srv._page_retry_at = time.monotonic() + _NEGATIVE_CACHE_SECONDS
                return False

    def page_get(self) -> None:
        if not self._page_ready():
            self._page_disabled()
            return
        self._guarded(ProductionHandler.do_GET)

    def page_post(self) -> None:
        if not self._page_ready():
            self._page_disabled()
            return
        self._guarded(ProductionHandler.do_POST)

    def _guarded(self, method) -> None:
        """His handler answers its own error kinds; anything else (a
        RecursionError from a deeply nested JSON body, a credentials refresh
        failure on a rotated key, ...) would otherwise kill the thread and
        drop the connection. Answer his 'unavailable' shape instead and log
        the exception TYPE only (never the request, never a token)."""
        try:
            method(self)
        except Exception as exc:  # noqa: BLE001
            print(f"  survey page: request failed ({type(exc).__name__})")
            try:
                self._reply(503, {"error": "unavailable",
                                  "message": "We couldn't confirm the save. Please retry."})
            except Exception:  # noqa: BLE001 (headers already sent, or the client left)
                pass

    def _page_disabled(self) -> None:
        self._reply(503, {"status": "disabled"})

    def _survey_shell(self) -> bytes:
        """His version reads the HTML with the host's default encoding (fails on
        a cp1252 Windows box: the page has a '●'). Same bytes, UTF-8 explicitly."""
        from html import escape
        from pathlib import Path
        from . import native_survey_http
        body = (Path(native_survey_http.__file__).with_name("native_survey_page.html")
                .read_text(encoding="utf-8"))
        return body.replace("__SURVEY_SUPPORT_EMAIL__",
                            escape(self.server.support_email, quote=True)).encode("utf-8")
