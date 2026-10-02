"""Pure native survey rules. No client configuration, datasets, or provider I/O."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
import secrets

SCHEMA_VERSION = 1
MESSAGE_STATES = ("planned", "submission_requested", "accepted", "sent_confirmed",
                  "delivered", "failed", "uncertain", "cancelled")


class SurveyError(ValueError):
    """Invalid request; messages deliberately omit answer and identity values."""


class Conflict(SurveyError):
    pass


class UnsupportedVersion(SurveyError):
    pass


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise SurveyError("invalid identifier")
    return value


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def timestamp(now: datetime) -> str:
    if now.tzinfo is None:
        raise SurveyError("timezone required")
    return now.astimezone(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class GymContext:
    """Construct ONLY from trusted server configuration, never request fields."""
    gym_id: str

    def __post_init__(self):
        identifier(self.gym_id)


def subject_id(ctx: GymContext, source: str, customer_id: str) -> str:
    identifier(source)
    return digest([ctx.gym_id, identifier(customer_id)])


def occurrence_id(ctx: GymContext, source: str, customer_id: str,
                  survey_type: str, cycle_id: str | None = None) -> str:
    key = [ctx.gym_id, identifier(source), identifier(customer_id)]
    if survey_type == "ftv" and cycle_id is None:
        return digest(key + ["first_visit"])
    if survey_type == "member" and cycle_id:
        return digest(key + ["member", identifier(cycle_id)])
    raise SurveyError("invalid survey occurrence")


@dataclass(frozen=True)
class Condition:
    question: str
    operator: str                 # eq, contains, lte
    value: str | int


@dataclass(frozen=True)
class Question:
    id: str
    wording: str
    kind: str                    # rating, single, multi, text
    required: bool = True
    version: int = 1
    minimum: int = 0
    maximum: int = 10
    options: tuple[tuple[str, str], ...] = ()
    visible_if: Condition | None = None
    max_length: int = 4000


@dataclass(frozen=True)
class Definition:
    id: str
    version: int
    survey_type: str
    questions: tuple[Question, ...]

    def __post_init__(self):
        identifier(self.id)
        if self.version < 1 or self.survey_type not in {"ftv", "member"}:
            raise SurveyError("invalid definition")
        seen = {}
        for q in self.questions:
            identifier(q.id)
            if q.id in seen or q.kind not in {"rating", "single", "multi", "text"}:
                raise SurveyError("invalid question")
            if q.version < 1 or q.minimum > q.maximum or q.max_length < 1:
                raise SurveyError("invalid question bounds")
            opts = [identifier(o[0]) for o in q.options]
            if len(opts) != len(set(opts)) or (q.kind in {"single", "multi"} and not opts):
                raise SurveyError("invalid options")
            c = q.visible_if
            if c:
                parent = seen.get(c.question)
                if parent is None or c.operator not in {"eq", "contains", "lte"}:
                    raise SurveyError("conditions must reference an earlier question")
                if (c.operator == "contains" and parent.kind != "multi"
                        or c.operator == "lte" and parent.kind != "rating"):
                    raise SurveyError("invalid condition type")
                if c.operator == "lte" and type(c.value) is not int:
                    raise SurveyError("invalid condition value")
                if c.operator == "contains" and c.value not in {o[0] for o in parent.options}:
                    raise SurveyError("invalid condition option")
            seen[q.id] = q

    @property
    def fingerprint(self) -> str:
        return digest(asdict(self))


def applicability(definition: Definition, answers: dict) -> dict[str, bool]:
    visible = {}
    for q in definition.questions:
        c = q.visible_if
        applies = True
        if c:
            parent = answers.get(c.question, {})
            applies = visible.get(c.question, False) and parent.get("status") == "answered"
            if applies:
                value = parent["value"]
                applies = ((value == c.value) if c.operator == "eq" else
                           (c.value in value) if c.operator == "contains" else
                           (value <= c.value))
        visible[q.id] = bool(applies)
    return visible


def validate_answer(q: Question, entry: dict) -> dict:
    if not isinstance(entry, dict) or set(entry) - {"status", "value"}:
        raise SurveyError("invalid answer structure")
    status = entry.get("status")
    if status in {"skipped", "cleared"}:
        if "value" in entry or (status == "skipped" and q.required):
            raise SurveyError("invalid skip or clear")
        return {"status": status}
    if status != "answered" or "value" not in entry:
        raise SurveyError("invalid answer status")
    value = entry["value"]
    opts = {o[0] for o in q.options}
    valid = False
    if q.kind == "rating":
        valid = type(value) is int and q.minimum <= value <= q.maximum
    elif q.kind == "single":
        valid = isinstance(value, str) and value in opts
    elif q.kind == "multi":
        valid = (isinstance(value, list) and all(isinstance(v, str) and v in opts for v in value)
                 and len(value) == len(set(value)) and (not q.required or bool(value)))
    elif q.kind == "text":
        valid = isinstance(value, str) and len(value) <= q.max_length and (not q.required or bool(value.strip()))
    if not valid:
        raise SurveyError("invalid answer value")
    return deepcopy(entry)       # preserve original text, including whitespace


def new_session(ctx, source, customer_id, definition, now, cycle_id=None):
    sid = occurrence_id(ctx, source, customer_id, definition.survey_type, cycle_id)
    return {
        "schema_version": SCHEMA_VERSION, "gym_id": ctx.gym_id, "id": sid,
        "subject_id": subject_id(ctx, source, customer_id),
        "survey_type": definition.survey_type, "cycle_id": cycle_id,
        "definition_id": definition.id, "definition_version": definition.version,
        "definition_hash": definition.fingerprint, "dispatch_owner": "native",
        "answers": {}, "applicability": applicability(definition, {}),
        "revision": 0, "state": "unanswered", "created_at": timestamp(now),
        "started_at": None, "last_saved_at": None, "completed_at": None,
        "completion": None, "assignment": None,
    }


def transition(session: dict, definition: Definition, command: dict, now: datetime):
    """Validate all answers before producing a new state; caller commits atomically."""
    if not isinstance(command, dict) or set(command) - {"kind", "expected_revision", "answers"}:
        raise SurveyError("invalid command")
    expected = command.get("expected_revision")
    if type(expected) is not int or expected != session["revision"]:
        raise Conflict("stale revision")
    if session["state"] == "completed":
        raise Conflict("survey already completed")
    result = deepcopy(session)
    stamp = timestamp(now)
    if stamp < (session["last_saved_at"] or session["created_at"]):
        raise SurveyError("clock moved backwards")
    kind = command.get("kind")
    changes = command.get("answers", {})
    if not isinstance(changes, dict):
        raise SurveyError("invalid answer batch")
    if kind == "answer":
        if not changes or set(changes) - {q.id for q in definition.questions}:
            raise SurveyError("unknown or empty answers")
        # Definition order allows one atomic batch to select a module and answer it.
        for q in definition.questions:
            if q.id not in changes:
                continue
            if not applicability(definition, result["answers"])[q.id]:
                raise SurveyError("question is not applicable")
            entry = validate_answer(q, changes[q.id])
            result["answers"][q.id] = {**entry, "question_version": q.version,
                                      "answered_at": stamp}
        result["applicability"] = applicability(definition, result["answers"])
        # Even a cleared answer retains the fact the session was started.
        if any(a["status"] == "answered" for a in result["answers"].values()):
            result["started_at"] = result["started_at"] or stamp
        result["state"] = "partial" if result["started_at"] else "unanswered"
    elif kind == "complete":
        if changes:
            raise SurveyError("save answers before completion")
        for q in definition.questions:
            if not result["applicability"][q.id]:
                continue
            status = result["answers"].get(q.id, {}).get("status")
            if status != "answered" and (q.required or status != "skipped"):
                raise SurveyError("applicable question requires answer or explicit optional skip")
        result["state"] = "completed"
        result["completed_at"] = stamp
        result["completion"] = {"answers": deepcopy(result["answers"]),
                                "applicability": dict(result["applicability"]),
                                "revision": result["revision"] + 1,
                                "completed_at": stamp,
                                "definition_hash": definition.fingerprint}
    else:
        raise SurveyError("unknown command")
    result["revision"] += 1
    result["last_saved_at"] = stamp
    return result


@dataclass(frozen=True)
class Experiment:
    id: str
    version: int = 1
    variants: tuple[str, ...] = ("control", "treatment")
    outcome_seconds: int = 7 * 24 * 60 * 60

    def __post_init__(self):
        identifier(self.id)
        if self.version < 1 or not self.variants or len(set(self.variants)) != len(self.variants):
            raise SurveyError("invalid experiment")
        for variant in self.variants:
            identifier(variant)
        if self.outcome_seconds <= 0:
            raise SurveyError("invalid outcome window")


def assignment(experiment: Experiment, session_id: str, now: datetime,
               draw: int | None = None) -> dict:
    # Draw outside transaction callbacks; persist once, never hash an email/template.
    index = secrets.randbelow(len(experiment.variants)) if draw is None else draw
    if type(index) is not int or not 0 <= index < len(experiment.variants):
        raise SurveyError("invalid random draw")
    result = {"experiment_id": experiment.id, "config_version": experiment.version,
            "config_hash": digest(asdict(experiment)), "variant": experiment.variants[index],
            "enrolled_at": timestamp(now), "primary_session_id": session_id,
            "randomization_version": "uniform-v1", "outcome_seconds": experiment.outcome_seconds}
    if getattr(experiment, "pin_policy", False):
        # Match Firestore's array representation on the first return and reload.
        result["experiment_policy"] = json.loads(json.dumps(asdict(experiment), allow_nan=False))
    return result


def message_id(session_id: str, kind: str = "survey_invitation", policy_id=None, ordinal=None):
    if kind == "survey_invitation" and policy_id is None and ordinal is None:
        return digest([session_id, "initial"])
    if kind == "survey_reminder" and policy_id and type(ordinal) is int and ordinal > 0:
        return digest([session_id, identifier(policy_id), ordinal])
    raise SurveyError("invalid message identity")


def new_invitation(session, template_id, template_version, now):
    identifier(template_id)
    if type(template_version) is not int or template_version < 1:
        raise SurveyError("invalid template version")
    return {"schema_version": SCHEMA_VERSION, "gym_id": session["gym_id"],
            "id": message_id(session["id"]), "session_id": session["id"],
            "subject_id": session["subject_id"], "type": "survey_invitation",
            "assignment": deepcopy(session["assignment"]), "template_id": template_id,
            "template_version": template_version, "state": "planned",
            "planned_at": timestamp(now), "due_at": None, "deadline_at": None,
            "provider": {"name": None, "message_id": None, "requested_at": None,
                         "accepted_at": None, "sent_at": None, "delivered_at": None,
                         "failed_at": None, "uncertain_at": None},
            "attempts": [], "capabilities": {}}
