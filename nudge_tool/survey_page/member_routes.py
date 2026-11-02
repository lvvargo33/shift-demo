"""Member survey page routes (A2, built 2026-11-02). OURS, not Chris's.

Serves Chris's member page (member_page.html / .js / .css, copied verbatim
from his 10/21 package) from the dashboard, on the same Firestore records,
capability tokens and save engine as the FTV page. His own member_http.py /
member_repository.py are NOT used: they refuse to run outside his emulator
(member_controls.require) and need his member-cycle ledger, which we do not
keep. What we keep from them is the public state shape (_public_state) and
the request checks, so his page JS works unchanged.

Routes (all unauthenticated; the token is the key, like /survey):
  GET  /member-survey?token=        the page shell
  GET  /member-survey.js / .css      static assets
  GET  /member-survey/state?token=  the respondent's state (questions, pages,
                                    answers, applicability)
  POST /member-survey/answer        save (Bearer token, Origin must match)

A token for an FTV session gets 403 here, and a member token gets 422 on the
FTV /survey routes (his SurveyHandler._state is FTV only), so the two pages
never open each other's sessions.
"""
from __future__ import annotations

import json
from copy import deepcopy
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from .member_assets import ASSETS, MemberExperienceConfig
from .native_survey import SurveyError
from .native_survey_store import CapabilityError

MEMBER_PATHS = frozenset({"/member-survey", "/member-survey.js", "/member-survey.css",
                          "/member-survey/state", "/member-survey/answer"})
_STATIC = {"/member-survey.js": ("member_page.js", "text/javascript; charset=utf-8"),
           "/member-survey.css": ("member_page.css", "text/css; charset=utf-8")}
_HTML = "text/html; charset=utf-8"
_HERE = Path(__file__).resolve().parent


def experience_config(client) -> MemberExperienceConfig:
    """Display identity + modules from client.json survey.native.member
    (gym_name, gym_short_name, modules); Chris's Shift defaults otherwise."""
    cfg = dict((((getattr(client, "survey", None) or {}).get("native") or {}).get("member")) or {})
    kw = {}
    for k in ("gym_name", "gym_short_name", "accent", "background", "instrument"):
        if isinstance(cfg.get(k), str) and cfg[k].strip():
            kw[k] = cfg[k].strip()
    if isinstance(cfg.get("modules"), list):
        kw["modules"] = tuple(cfg["modules"])
    return MemberExperienceConfig(**kw)


def public_state(repository, token: str, config: MemberExperienceConfig) -> tuple[str, dict]:
    """(session_id, state) for a member token. Raises CapabilityError for a
    bad / expired / FTV token. Shape = Chris's MemberRepository._public_state
    (minus his cycle 'superseded' case: each of our member surveys is its own
    session, so an older link simply shows its own survey)."""
    cap = repository._authorize(token)
    if cap.get("candidate") is not None:
        raise CapabilityError()
    session = repository.get_session(cap["session_id"])  # checks the definition hash
    assets = ASSETS.get((session.get("definition_id"), session.get("definition_version")))
    if assets is None or session.get("survey_type") != "member":
        raise CapabilityError()
    definition, presentation, module_config, _ = assets
    questions = []
    for q in definition.questions:
        row = {"id": q.id, "wording": config.wording(q.wording), "kind": q.kind,
               "required": q.required, "version": q.version,
               "minimum": q.minimum, "maximum": q.maximum,
               "options": [list(o) for o in q.options], "max_length": q.max_length}
        if getattr(q, "exclusive_options", ()):
            row["exclusive_options"] = list(q.exclusive_options)
        if hasattr(q, "max_selections"):
            row["max_selections"] = q.max_selections
        questions.append(row)
    # His display rule: Shift pages show no "Core Metrics" / "Member Experience" /
    # "Staff recognition" headings (pins unchanged).
    shift = definition.id.startswith("shift_member_modules_")
    pages = [{"id": p.id,
              "title": "" if shift and p.title.casefold() in {
                  "core metrics", "member experience", "staff recognition"} else p.title,
              "module": p.module, "finish": p.finish,
              "groups": [{"id": g.id, "heading": g.heading, "questions": list(g.questions),
                          **({"helper_text": g.helper_text} if hasattr(g, "helper_text") else {})}
                         for g in p.groups]}
             for p in presentation.pages]
    completed = session.get("state") == "completed"
    return session["id"], {
        "revision": session["revision"], "state": session["state"],
        "answers": deepcopy(session["answers"]),
        "applicability": dict(session["applicability"]),
        "read_only": completed, "read_only_reason": "completed" if completed else None,
        "questions": questions, "pages": pages,
        "definition": {"id": definition.id, "version": definition.version,
                       "hash": definition.fingerprint},
        "presentation": {"id": presentation.id, "version": presentation.version,
                         "hash": presentation.fingerprint},
        "module_config": {"id": module_config.id, "version": module_config.version,
                          "hash": module_config.fingerprint},
    }


def shell(config: MemberExperienceConfig, support_email: str) -> bytes:
    """His _member_shell(), UTF-8 explicitly (cp1252 hosts)."""
    body = (_HERE / "member_page.html").read_text(encoding="utf-8")
    body = (body.replace("Appalachian Bouldering Center", escape(config.gym_name))
                .replace("APPALACHIAN BOULDERING CENTER", escape(config.gym_name.upper()))
                .replace("ABC better", escape(config.gym_short_name) + " better"))
    return body.replace("__SURVEY_SUPPORT_EMAIL__", escape(support_email, quote=True)).encode("utf-8")


def static_asset(path: str, config: MemberExperienceConfig) -> tuple[bytes, str]:
    name, ctype = _STATIC[path]
    data = (_HERE / name).read_bytes()
    if path == "/member-survey.css":
        data = (data.replace(b"#173b34", config.accent.encode())
                    .replace(b"#25654e", config.accent.encode())
                    .replace(b"#f3f1e8", config.background.encode()))
    return data, ctype


class MemberRoutesMixin:
    """Mixed into the mounted handler (needs his SurveyHandler helpers:
    _reply, _host_ok, _origin, _token, _error; and server.repository,
    server.support_email, server.member_config)."""

    def member_do_GET(self):
        if not self._host_ok():
            return self._reply(403, {"error": "host"})
        path = urlsplit(self.path)
        config = self.server.member_config
        if path.path in _STATIC:
            data, ctype = static_asset(path.path, config)
            return self._reply(200, data, ctype)
        if path.path not in {"/member-survey", "/member-survey/state"}:
            return self._reply(404, {"error": "not_found"})
        try:
            _, state = public_state(self.server.repository, self._token(path.query), config)
            if path.path == "/member-survey/state":
                return self._reply(200, state)
            return self._reply(200, shell(config, self.server.support_email), _HTML)
        except Exception as exc:  # noqa: BLE001 (his page shows link / unavailable itself)
            if path.path == "/member-survey":
                status = 403 if isinstance(exc, CapabilityError) else 503
                return self._reply(status, shell(config, self.server.support_email), _HTML)
            if isinstance(exc, SurveyError):
                return self._error(exc)
            raise

    def member_do_POST(self):
        path = urlsplit(self.path)
        if (self.headers.get_all("Origin") != [self._origin()]
                or self.headers.get("Transfer-Encoding") is not None
                or len(self.headers.get_all("Content-Length", [])) != 1
                or len(self.headers.get_all("Authorization", [])) != 1):
            return self._reply(403, {"error": "request"})
        if not self._host_ok():
            return self._reply(403, {"error": "origin",
                                     "message": "Open the original survey link to continue."})
        if path.path != "/member-survey/answer" or path.query:
            return self._reply(404, {"error": "not_found"})
        try:
            if self.headers.get("Content-Type") != "application/json":
                raise SurveyError("JSON required")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise SurveyError("body size")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict) or set(body) != {"request_id", "command"}:
                raise SurveyError("invalid body")
            token = self._token(path.query)
            config = self.server.member_config
            sid, _ = public_state(self.server.repository, token, config)
            receipt = self.server.repository.submit(token, sid, body["request_id"], body["command"])
            _, state = public_state(self.server.repository, token, config)
            return self._reply(200, {"receipt": {k: v for k, v in receipt.items() if k != "session_id"},
                                     "state": state})
        except (ValueError, TypeError) as exc:
            return self._error(exc if isinstance(exc, SurveyError) else SurveyError("invalid request"))
