"""Synthetic respondent HTTP surface; deliberately separate from live_server.

Only loopback binding, an emulator repository, and the reviewed FTV asset. No
dashboard fallback, provider, production configuration, or trusted mutation route.
"""
from dataclasses import asdict
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

from google.api_core.exceptions import GoogleAPICallError
from .native_survey import Conflict, SurveyError, UnsupportedVersion
from .native_survey_definitions import get_definition
from .native_survey_store import CapabilityError, RetryableStoreError, SurveyRepository

ASSETS = {
    "/survey": ("native_survey_page.html", "text/html; charset=utf-8"),
    "/survey.js": ("native_survey_page.js", "text/javascript; charset=utf-8"),
    "/survey.css": ("native_survey_page.css", "text/css; charset=utf-8"),
}


class SurveyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, repository, *, support_email=None):
        if address[0] != "127.0.0.1" or not isinstance(repository, SurveyRepository):
            raise ValueError("loopback emulator repository required")
        if support_email is not None and (len(support_email) > 254 or
                not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+", support_email)):
            raise ValueError("invalid support email")
        self.repository = repository
        self.support_email = support_email or ""
        super().__init__(address, SurveyHandler)


class SurveyHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # Default handler logs full URLs, including bearer capabilities.

    def _reply(self, status, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Lost acknowledgement: retry keeps the request ID.

    def _survey_shell(self):
        body = Path(__file__).with_name("native_survey_page.html").read_text()
        return body.replace("__SURVEY_SUPPORT_EMAIL__", escape(self.server.support_email, quote=True)).encode()

    def _host_ok(self):
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

    def _origin(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def _token(self, query):
        if self.command == "POST":
            header = self.headers.get("Authorization", "")
            return header[7:] if header.startswith("Bearer ") else None
        params = parse_qs(query, keep_blank_values=True)
        if set(params) != {"token"} or len(params["token"]) != 1:
            raise CapabilityError()
        return params["token"][0]

    def _state(self, token):
        state = self.server.repository.resume(token)
        if state["definition_id"] != "ftv":
            raise UnsupportedVersion("FTV only in this synthetic UI")
        definition = get_definition(state["definition_id"], state["definition_version"])
        # The private session locator never needs to be supplied by a browser.
        public = {k: v for k, v in state.items() if k != "session_id"}
        public["questions"] = [asdict(q) for q in definition.questions]
        return state["session_id"], public

    def _error(self, exc):
        if isinstance(exc, CapabilityError):
            return self._reply(403, {"error": "link", "message": "This survey link is invalid or has expired."})
        if isinstance(exc, Conflict):
            return self._reply(409, {"error": "conflict", "message": "Your survey changed in another tab. Load the saved answers before editing again."})
        if isinstance(exc, UnsupportedVersion):
            return self._reply(422, {"error": "version", "message": "This survey version is not available here."})
        if isinstance(exc, SurveyError):
            return self._reply(422, {"error": "validation", "message": "Check your answers before continuing."})
        return self._reply(503, {"error": "unavailable", "message": "We couldn't confirm the save. Please retry."})

    def do_GET(self):
        if not self._host_ok():
            return self._reply(403, {"error": "host"})
        path = urlsplit(self.path)
        if path.path in {"/survey.js", "/survey.css"}:
            # Public static assets contain no respondent state or credentials.
            name, ctype = ASSETS[path.path]
            return self._reply(200, Path(__file__).with_name(name).read_bytes(), ctype)
        if path.path not in {"/survey", "/survey/state"}:
            return self._reply(404, {"error": "not_found"})
        try:
            _, state = self._state(self._token(path.query))
            if path.path == "/survey/state":
                return self._reply(200, state)
            _, ctype = ASSETS["/survey"]
            return self._reply(200, self._survey_shell(), ctype)
        except (SurveyError, RetryableStoreError, GoogleAPICallError) as exc:
            # Render the static shell on entry errors so the UI can offer a clear
            # link/unavailable state. Its read-only state request repeats auth.
            if path.path == "/survey":
                status = 403 if isinstance(exc, CapabilityError) else 503
                return self._reply(status, self._survey_shell(), "text/html; charset=utf-8")
            return self._error(exc)

    def do_POST(self):
        path = urlsplit(self.path)
        origin = self._origin()
        if not self._host_ok() or self.headers.get("Origin") != origin:
            return self._reply(403, {"error": "origin", "message": "Open the original survey link to continue."})
        if path.path != "/survey/answer" or path.query:
            return self._reply(404, {"error": "not_found"})
        try:
            if self.headers.get("Content-Type") != "application/json":
                raise SurveyError("JSON required")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 32768:
                raise SurveyError("body size")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict) or set(body) != {"request_id", "command"}:
                raise SurveyError("invalid body")
            token = self._token(path.query)
            sid, _ = self._state(token)
            result = self.server.repository.submit(token, sid, body["request_id"], body["command"])
            # Return the receipt, not an unrelated newer revision after a race.
            return self._reply(200, {k: v for k, v in result.items() if k != "session_id"})
        except (ValueError, TypeError) as exc:
            return self._error(exc if isinstance(exc, SurveyError) else SurveyError("invalid request"))
        except (RetryableStoreError, GoogleAPICallError) as exc:
            return self._error(exc)
