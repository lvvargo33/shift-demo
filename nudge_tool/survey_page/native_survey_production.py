"""Explicit production storage/configuration boundary; never loaded by the harness."""
from dataclasses import dataclass
import ipaddress
import os
import re
from urllib.parse import urlencode, urlsplit

import google.auth
from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore

from .native_survey import GymContext, SurveyError
from .native_survey_store import _SurveyOperations


@dataclass(frozen=True)
class ProductionConfig:
    gym_id: str
    project_id: str
    database_id: str
    public_base_url: str
    support_email: str

    def __post_init__(self):
        GymContext(self.gym_id)
        if re.search(r"(^|[-_])(test|synthetic|demo)([-_]|$)", self.gym_id, re.I):
            raise SurveyError("production gym required")
        if (not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", self.project_id)
                or self.project_id.startswith("demo-")):
            raise SurveyError("production project required")
        if self.database_id != "(default)" and not re.fullmatch(r"[a-z][a-z0-9-]{2,61}[a-z0-9]", self.database_id):
            raise SurveyError("invalid database")
        raw_origin = self.public_base_url
        if (not isinstance(raw_origin, str)
                or any(ord(character) < 32 or ord(character) == 127
                       for character in raw_origin)):
            raise SurveyError("canonical HTTPS public origin required")
        try:
            url = urlsplit(raw_origin)
        except ValueError:
            raise SurveyError("canonical HTTPS public origin required") from None
        if (url.scheme != "https" or not url.hostname or "." not in url.hostname
                or url.username or url.password or url.path or url.query or url.fragment
                or url.netloc != url.hostname or url.hostname.endswith(".localhost")):
            raise SurveyError("canonical HTTPS public origin required")
        if (len(url.hostname) > 253 or any(not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in url.hostname.split("."))):
            raise SurveyError("invalid public hostname")
        try:
            ipaddress.ip_address(url.hostname)
        except ValueError:
            pass
        else:
            raise SurveyError("public DNS hostname required")
        canonical_origin = "https://" + url.hostname
        if raw_origin != canonical_origin:
            # Exact comparison catches parser-normalized scheme/host case, empty
            # query/fragment markers, whitespace, trailing dots/slashes and ports.
            raise SurveyError("canonical HTTPS public origin required")
        if (len(self.support_email) > 254 or not re.fullmatch(
                r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+",
                self.support_email)):
            raise SurveyError("configured support email required")

    @property
    def public_origin(self):
        """The sole validated origin representation used by public behavior."""
        return self.public_base_url

    @property
    def public_host(self):
        return self.public_origin.removeprefix("https://")

    @classmethod
    def from_environment(cls, env=None):
        env = os.environ if env is None else env
        if env.get("NATIVE_SURVEY_ENABLED") != "true":
            raise SurveyError("native survey production service disabled")
        if "FIRESTORE_EMULATOR_HOST" in env:
            raise SurveyError("emulator environment forbidden in production")
        return cls(env.get("NATIVE_SURVEY_GYM_ID", ""),
                   env.get("FIRESTORE_PROJECT_ID", ""),
                   env.get("FIRESTORE_DATABASE_ID", "(default)"),
                   env.get("NATIVE_SURVEY_PUBLIC_BASE_URL", ""),
                   env.get("NATIVE_SURVEY_SUPPORT_EMAIL", ""))


class ProductionRepository(_SurveyOperations):
    def __init__(self, config: ProductionConfig, *, credentials):
        if not isinstance(config, ProductionConfig):
            raise SurveyError("production configuration required")
        if "FIRESTORE_EMULATOR_HOST" in os.environ:
            raise SurveyError("emulator environment forbidden in production")
        if credentials is None or isinstance(credentials, AnonymousCredentials):
            raise SurveyError("authenticated server credentials required")
        self._context = GymContext(config.gym_id)
        self._db = firestore.Client(project=config.project_id,
                                    database=config.database_id, credentials=credentials)
        self._root = self._db.collection("gyms").document(config.gym_id)


def create_production_repository(config):
    """Only the production entry point invokes ADC. Tests inject credentials/client."""
    if not isinstance(config, ProductionConfig) or "FIRESTORE_EMULATOR_HOST" in os.environ:
        raise SurveyError("production configuration without emulator required")
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/datastore"])
    return ProductionRepository(config, credentials=credentials)


def survey_url(config, repository, capability):
    """Trusted server helper. Candidate Q1 is already pinned inside the capability."""
    if repository._context.gym_id != config.gym_id:
        raise SurveyError("URL repository gym mismatch")
    repository.resume(capability)  # authorize, including expiry; read only
    return config.public_origin + "/survey?" + urlencode({"token": capability})
