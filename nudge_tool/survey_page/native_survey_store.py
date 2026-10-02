"""Shared native operations and independently guarded emulator constructor.

One gym-bound repository; three collections beneath gyms/{gym}, plus session events.
Public methods are trusted server primitives, not HTTP endpoints. Untrusted callers
must use resume/submit with a capability, never call mutate with a supplied identity.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
import re
import secrets

from google.auth.credentials import AnonymousCredentials
from google.api_core.exceptions import Aborted, DeadlineExceeded, ServiceUnavailable
from google.cloud import firestore

from .native_survey import (
    SCHEMA_VERSION, Conflict, GymContext, SurveyError, UnsupportedVersion,
    assignment, digest, identifier, new_invitation, new_session, timestamp, transition,
)
from .native_survey_definitions import get_definition


class CapabilityError(SurveyError):
    def __init__(self):
        super().__init__("invalid or expired survey capability")


class RetryableStoreError(RuntimeError):
    """Commit outcome may be unknown. Retry the SAME request ID and body."""


class _SurveyOperations:
    """Shared operations; only explicit emulator/production constructors bind clients."""

    def __init__(self):
        raise TypeError("use an explicit repository boundary")

    def _ref(self, group, key):
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
            raise SurveyError("invalid record identifier")
        return self._root.collection(group).document(key)

    def _checked(self, snapshot):
        if not snapshot.exists:
            raise SurveyError("record not found")
        doc = snapshot.to_dict()
        if doc.get("gym_id") != self._context.gym_id:
            raise SurveyError("gym mismatch")
        if doc.get("schema_version") != SCHEMA_VERSION:
            raise UnsupportedVersion("unsupported record schema")
        return doc

    def _run(self, operation):
        try:
            return operation(self._db.transaction(max_attempts=8))
        except (Aborted, DeadlineExceeded, ServiceUnavailable):
            raise RetryableStoreError("storage retry required; preserve request identity") from None
        except ValueError as exc:
            # SDK wraps exhausted transaction retries in ValueError with Aborted
            # as its cause. Domain validation/conflict errors must pass through.
            if isinstance(exc.__cause__, Aborted):
                raise RetryableStoreError("transaction contention; retry same request") from None
            raise

    @staticmethod
    def _definition(session):
        definition = get_definition(session["definition_id"], session["definition_version"])
        if definition.fingerprint != session["definition_hash"]:
            raise UnsupportedVersion("survey definition changed without version bump")
        return definition

    def create_session(self, source, customer_id, definition_id="ftv", version=1,
                       *, cycle_id=None, experiment=None):
        definition = get_definition(definition_id, version)
        now = datetime.now(timezone.utc)
        session = new_session(self._context, source, customer_id, definition, now, cycle_id)
        session_ref = self._ref("sessions", session["id"])
        subject_ref = self._ref("subjects", session["subject_id"])
        proposed = assignment(experiment, session["id"], now) if experiment else None

        @firestore.transactional
        def create(tx):
            ss = session_ref.get(transaction=tx)
            ps = subject_ref.get(transaction=tx)
            if ss.exists:
                existing = self._checked(ss)
                self._definition(existing)
                wanted = experiment.id if experiment else None
                if (existing.get("assignment") or {}).get("experiment_id") != wanted:
                    raise Conflict("cannot change enrollment of existing occurrence")
                if experiment and existing["assignment"]["config_hash"] != proposed["config_hash"]:
                    raise Conflict("experiment version is already pinned")
                return existing
            subject = self._checked(ps) if ps.exists else {
                "schema_version": SCHEMA_VERSION, "gym_id": self._context.gym_id,
                "id": session["subject_id"], "identity_source": identifier(source),
                "customer_id": identifier(customer_id), "assignments": {},
                "feedback_suppression": {"suppressed": False, "source": None},
                "member_cycles": {}, "dispatch_reservation": None,
            }
            if subject["identity_source"] != source:
                raise Conflict("canonical identity source requires explicit reconciliation")
            result = deepcopy(session)
            if experiment:
                key = digest([experiment.id])
                saved = subject["assignments"].get(key)
                if (saved and getattr(experiment, "pin_policy", False)
                        and saved["primary_session_id"] != session["id"]):
                    raise Conflict("experiment already designates another occurrence")
                if saved and saved["config_hash"] != proposed["config_hash"]:
                    raise Conflict("experiment version is already pinned")
                if not saved and len(subject["assignments"]) >= 64:
                    raise SurveyError("pilot assignment limit reached")
                subject["assignments"][key] = saved or proposed
                result["assignment"] = deepcopy(subject["assignments"][key])
            tx.set(subject_ref, subject)
            tx.create(session_ref, result)
            return result

        return self._run(create)

    def get_session(self, session_id):
        session = self._checked(self._ref("sessions", session_id).get())
        self._definition(session)
        return session

    def get_subject(self, subject_id):
        return self._checked(self._ref("subjects", subject_id).get())

    def get_message(self, message_id):
        return self._checked(self._ref("messages", message_id).get())

    def create_invitation(self, session_id, template_id, template_version):
        session_ref = self._ref("sessions", session_id)

        @firestore.transactional
        def create(tx):
            session = self._checked(session_ref.get(transaction=tx))
            self._definition(session)
            proposed = new_invitation(session, template_id, template_version, datetime.now(timezone.utc))
            ref = self._ref("messages", proposed["id"])
            snap = ref.get(transaction=tx)
            if snap.exists:
                return self._checked(snap)  # template remains pinned, not overwritten
            tx.create(ref, proposed)
            return proposed

        return self._run(create)

    def events(self, session_id):
        self.get_session(session_id)
        docs = self._ref("sessions", session_id).collection("events").stream()
        return sorted((self._checked(d) for d in docs), key=lambda e: e["revision"])

    def mutate(self, session_id, request_id, command):
        """Trusted internal command; public callers use submit() for authorization."""
        return self._mutate(session_id, request_id, command, token=None, trusted=True)

    def _mutate(self, session_id, request_id, command, *, token, trusted=False):
        identifier(request_id)
        if not isinstance(command, dict):
            raise SurveyError("invalid command")
        body = deepcopy(command)
        request_digest = digest(body)
        ref = self._ref("sessions", session_id)
        event_ref = ref.collection("events").document(request_id)

        @firestore.transactional
        def save(tx):
            cap = None
            if not trusted:
                cap = self._authorize(token, session_id, tx=tx)
            session = self._checked(ref.get(transaction=tx))
            definition = self._definition(session)
            receipt = event_ref.get(transaction=tx)
            if receipt.exists:
                previous = self._checked(receipt)
                if previous["request_digest"] != request_digest:
                    raise Conflict("idempotency key reused with different body")
                return previous["result"]
            effective = body
            if body.get("kind") == "confirm_q1_q2":
                if (not cap or definition.id != "ftv" or
                        set(body) != {"kind", "expected_revision", "q2", "edited_q1"}):
                    raise SurveyError("invalid embedded confirmation")
                persisted = session["answers"].get("q1")
                q1 = body["edited_q1"]
                if q1 is None:
                    q1 = persisted.get("value") if persisted else cap["candidate"]
                effective = {"kind": "answer", "expected_revision": body["expected_revision"],
                             "answers": {"q1": {"status": "answered", "value": q1},
                                         "q2": {"status": "answered", "value": body["q2"]}}}
            updated = transition(session, definition, effective, datetime.now(timezone.utc))
            result = {"session_id": session_id, "revision": updated["revision"],
                      "state": updated["state"], "saved_at": updated["last_saved_at"],
                      "completed_at": updated["completed_at"]}
            changes = effective.get("answers", {})
            event = {"schema_version": SCHEMA_VERSION, "gym_id": self._context.gym_id,
                     "session_id": session_id, "kind": body["kind"],
                     "revision": updated["revision"], "recorded_at": result["saved_at"],
                     "request_digest": request_digest, "result": result,
                     "answers": {key: updated["answers"][key] for key in changes},
                     "previous_answers": {key: session["answers"].get(key) for key in changes},
                     "definition_hash": session["definition_hash"],
                     "message_id": token.split(".")[0] if token else None}
            tx.set(ref, updated)
            tx.create(event_ref, event)
            return result

        return self._run(save)

    def issue_capability(self, message_id, *, candidate=None, lifetime=timedelta(days=30)):
        """Return plaintext once. Replacement issuance leaves earlier hashes valid."""
        if not timedelta(0) < lifetime <= timedelta(days=90):
            raise SurveyError("invalid capability lifetime")
        secret = secrets.token_urlsafe(32)
        token = message_id + "." + secret
        hashed = hashlib.sha256(token.encode()).hexdigest()
        ref = self._ref("messages", message_id)
        now = datetime.now(timezone.utc)

        @firestore.transactional
        def issue(tx):
            message = self._checked(ref.get(transaction=tx))
            session = self._checked(self._ref("sessions", message["session_id"]).get(transaction=tx))
            definition = self._definition(session)
            if candidate is not None:
                if definition.id != "ftv" or type(candidate) is not int or not 1 <= candidate <= 5:
                    raise SurveyError("invalid embedded Q1 candidate")
            if len(message["capabilities"]) >= 32:
                raise SurveyError("pilot capability limit reached; no tokens revoked")
            cap = {"gym_id": self._context.gym_id, "session_id": session["id"],
                   "message_id": message_id, "scope": "survey", "candidate": candidate,
                   "issued_at": timestamp(now), "expires_at": timestamp(now + lifetime)}
            message["capabilities"][hashed] = cap
            tx.set(ref, message)

        self._run(issue)
        return token

    @staticmethod
    def _validate_token(token):
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}\.[A-Za-z0-9_-]{43}", token):
            raise CapabilityError()

    def _authorize(self, token, expected_session=None, tx=None):
        self._validate_token(token)
        message_id = token.split(".")[0]
        snap = self._ref("messages", message_id).get(transaction=tx)
        if not snap.exists:
            raise CapabilityError()
        message = self._checked(snap)
        hashed = hashlib.sha256(token.encode()).hexdigest()
        cap = next((v for k, v in message["capabilities"].items() if hmac.compare_digest(k, hashed)), None)
        if (not cap or cap["gym_id"] != self._context.gym_id or cap["scope"] != "survey"
                or cap["message_id"] != message_id or cap["session_id"] != message["session_id"]
                or (expected_session and cap["session_id"] != expected_session)
                or cap["expires_at"] <= timestamp(datetime.now(timezone.utc))):
            raise CapabilityError()
        return cap

    def resume(self, token, *, expected_session=None, mid=None):
        """GET model: reads only. Returned candidate is not an answer or event."""
        cap = self._authorize(token, expected_session)
        session = self.get_session(cap["session_id"])
        if mid is not None:
            subject = self.get_subject(session["subject_id"])
            if mid != subject["customer_id"]:
                raise CapabilityError()
        q1 = session["answers"].get("q1")
        candidate = q1.get("value") if q1 else cap["candidate"]
        # Return respondent state only, never subject/assignment/message provenance.
        return {"session_id": session["id"], "revision": session["revision"],
                "state": session["state"], "answers": session["answers"],
                "applicability": session["applicability"], "q1_candidate": candidate,
                "definition_id": session["definition_id"],
                "definition_version": session["definition_version"],
                "read_only": session["state"] == "completed"}

    def submit(self, token, session_id, request_id, command):
        """Future POST model: validate capability in the same mutation transaction."""
        self._validate_token(token)  # reject missing/malformed authority before mutation
        return self._mutate(session_id, request_id, command, token=token)

    def confirm_q1_q2(self, token, session_id, request_id, expected_revision, q2, *, edited_q1=None):
        # Resolve candidate inside the transaction, AFTER the receipt lookup so a
        # retry returns the original result even after a later correction.
        return self.submit(token, session_id, request_id, {
            "kind": "confirm_q1_q2", "expected_revision": expected_revision,
            "q2": q2, "edited_q1": edited_q1})


class SurveyRepository(_SurveyOperations):
    def __init__(self, context: GymContext, *, project="demo-sendit-survey"):
        if not isinstance(context, GymContext):
            raise SurveyError("server gym context required")
        host = os.environ.get("FIRESTORE_EMULATOR_HOST", "")
        match = re.fullmatch(r"127\.0\.0\.1:([0-9]{1,5})", host)
        if not match or not 1 <= int(match[1]) <= 65535 or not re.fullmatch(r"demo-[a-z0-9-]+", project):
            raise SurveyError("loopback emulator and demo project required")
        if not context.gym_id.endswith("-test"):
            raise SurveyError("synthetic gym context required")
        self._context = context
        # Explicit anonymous credentials: never consult ADC, .env or service accounts.
        self._db = firestore.Client(project=project, credentials=AnonymousCredentials())
        self._root = self._db.collection("gyms").document(context.gym_id)
