"""Configuration loader for the SHIFT nudge tool.

The tool is generic; everything client-specific lives in
nudge_tool/clients/<slug>/client.json + templates/. This module turns that
folder into a ClientConfig the engine can run. Nothing here makes a network
call. To onboard a new client: copy a clients/<slug> folder, edit client.json
and the templates, drop the client's .env next to it.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .templates import Template, load_templates
from .triggers import Trigger, triggers_from_config

# nudge_tool/ -> SHIFT_Automation/. Relative data/env paths resolve from here.
TOOL_DIR = Path(__file__).resolve().parent
BASE_DIR = TOOL_DIR.parent
CLIENTS_DIR = TOOL_DIR / "clients"
DASHBOARD_TEMPLATE = TOOL_DIR / "dashboard_template.html"


def _resolve(path_str: str) -> Path:
    """Absolute path as-is; otherwise relative to SHIFT_Automation."""
    p = Path(path_str)
    return p if p.is_absolute() else (BASE_DIR / p)


class ESPError(Exception):
    """Base for email-service-provider backend errors. MailchimpError and
    BrevoError both subclass it so cli.py / send_nudges.py can catch one type
    regardless of which ESP a client uses."""


@dataclass(frozen=True)
class Settings:
    """ESP credentials. Mailchimp uses server + audience_id; Brevo uses only the
    api_key plus a send-from identity. Fields the other ESP does not need stay at
    their defaults, so both backends share one Settings shape."""
    api_key: str
    server: str = ""
    audience_id: str = ""
    esp: str = "mailchimp"
    sender_email: str = ""   # brevo: the From address for transactional sends
    sender_name: str = ""    # brevo: the From display name
    reply_to_email: str = ""  # brevo: where a recipient's Reply goes. Blank =
                              # omit the field so Brevo replies to the sender.

    @property
    def api_base(self) -> str:
        return f"https://{self.server}.api.mailchimp.com/3.0"


@dataclass
class ClientConfig:
    slug: str
    client_name: str
    send_from_name: str           # brevo From display name (defaults to client_name if blank)
    live_enabled: bool
    min_days_between_sends: int   # S38 spacing guard: skip anyone whose last
                                  # logged send (any trigger) is fewer than this
                                  # many days old. 0 = off.
    esp: str                      # send backend: "mailchimp" (default) or "brevo"
    env_file: Path
    transactions_csv: Path
    memberships_csv: Path
    sessions_csv: Path
    member_status_csv: Path       # membership-survey audience source (guid,
                                  # name, email, MEMBER status, doNotMail).
                                  # Optional: absent for clients that do not
                                  # run membership surveys.
    trial_skus: list[str]
    low_commitment_skus: list[str]
    conversion_skus: list[str]
    daypass_repeat: dict          # {"min_count": int, "window_days": int}
    reporting: dict               # workbook-aligned reporting defs (cohort/entry-product/staff/conversion)
    survey: dict                  # survey loop config (S2); {} or {"enabled": False} = off
    member_survey: dict           # membership-survey loop; its OWN enable gate,
                                  # so turning FTV sending on does not start
                                  # mailing members. {} = off. See
                                  # nudge_tool/member_survey.py.
    links: dict                   # client link/CTA tokens substituted into templates ({token}: url/text)
    test_contacts: dict           # S5 --test config; {"base_email": str, "max": int}
    triggers: list[Trigger]
    templates: dict[str, Template]
    client_dir: Path

    @property
    def out_dir(self) -> Path:
        return self.client_dir / "out"

    @property
    def data_json(self) -> Path:
        return self.out_dir / "data.json"

    @property
    def dashboard_html(self) -> Path:
        return self.out_dir / "dashboard.html"

    @property
    def outreach_log(self) -> Path:
        return self.client_dir / "outreach_log.csv"

    @property
    def member_followups_csv(self) -> Path:
        """Membership-survey follow-up state (status / assignee / notes per
        response). Append-only CSV next to the outreach log, synced to Drive
        the same way. See nudge_tool/member_followups.py."""
        return self.client_dir / "member_followups.csv"

    @property
    def member_counts_csv(self) -> Path:
        """Weekly member-count snapshots, one row per ISO week, appended by
        the daily cron. This file IS the member trend line: the roster CSV is
        a snapshot with no history and most active members carry no dates, so
        the trend cannot be rebuilt after the fact. See
        nudge_tool/member_counts.py for the measurements behind that."""
        return self.client_dir / "member_counts.csv"

    @property
    def member_reply_edits_csv(self) -> Path:
        """Reply edit pairs (template text vs what a staffer actually sent),
        one row per reply sent from the response page. Append-only, synced
        to Drive like the follow-ups. See nudge_tool/member_reply_edits.py."""
        return self.client_dir / "member_reply_edits.csv"


def load_client(slug: str) -> ClientConfig:
    client_dir = CLIENTS_DIR / slug
    cfg_path = client_dir / "client.json"
    if not cfg_path.exists():
        raise RuntimeError(f"No client config at {cfg_path}")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    data = cfg.get("data", {})
    return ClientConfig(
        slug=cfg.get("slug", slug),
        client_name=cfg.get("client_name", slug),
        send_from_name=cfg.get("send_from_name", ""),
        live_enabled=bool(cfg.get("live_enabled", False)),
        min_days_between_sends=int(cfg.get("min_days_between_sends", 0)),
        esp=str(cfg.get("esp", "mailchimp")).strip().lower() or "mailchimp",
        env_file=_resolve(cfg.get("env_file", ".env")),
        transactions_csv=_resolve(data["transactions_csv"]),
        memberships_csv=_resolve(data["memberships_csv"]),
        sessions_csv=_resolve(data["sessions_csv"]),
        # .get with a default, not data[...]: a client.json written before
        # membership surveys existed must still load.
        member_status_csv=_resolve(data.get("member_status_csv",
                                            "data/member_status.csv")),
        trial_skus=[s.lower() for s in cfg.get("trial_skus", [])],
        low_commitment_skus=[s.lower() for s in cfg.get("low_commitment_skus", [])],
        conversion_skus=[s.lower() for s in cfg.get("conversion_skus", [])],
        daypass_repeat=dict(cfg.get("daypass_repeat", {"min_count": 2, "window_days": 30})),
        reporting=dict(cfg.get("reporting", {})),
        survey=dict(cfg.get("survey", {})),
        member_survey=dict(cfg.get("member_survey", {})),
        links=dict(cfg.get("links", {})),
        test_contacts=dict(cfg.get("test_contacts", {})),
        triggers=triggers_from_config(cfg.get("triggers", [])),
        templates=load_templates(client_dir / "templates"),
        client_dir=client_dir,
    )


def load_settings(client: ClientConfig, require: bool = True) -> Settings:
    """Load this client's ESP credentials from their env_file.

    Branches on the client's `esp`: Brevo reads BREVO_API_KEY (+ an optional
    BREVO_SEND_FROM / BREVO_SEND_FROM_NAME send-from identity and an optional
    BREVO_REPLY_TO); Mailchimp reads the existing MAILCHIMP_* trio. Defaults to
    Mailchimp for back-compat.

    Reply-To defaults to the send-from address (2026-08-20 decision: ABC uses
    hello@appalachianboulderingcenter.com for BOTH), so setting BREVO_SEND_FROM
    alone is enough and BREVO_REPLY_TO only exists to split the two apart. When
    neither is set the value stays blank and brevo_client omits the field, which
    leaves Brevo's own default (reply to the sender) in place. That keeps this
    change a no-op for any client that has not configured a send-from yet."""
    load_dotenv(client.env_file, override=True)

    if client.esp == "brevo":
        api_key = os.getenv("BREVO_API_KEY", "").strip()
        sender_email = os.getenv("BREVO_SEND_FROM", "").strip()
        sender_name = (os.getenv("BREVO_SEND_FROM_NAME", "").strip()
                       or client.send_from_name.strip()
                       or client.client_name)
        reply_to_email = os.getenv("BREVO_REPLY_TO", "").strip() or sender_email
        if require and not api_key:
            raise RuntimeError(
                f"Missing credential(s) in {client.env_file}: BREVO_API_KEY"
            )
        return Settings(api_key=api_key, esp="brevo",
                        sender_email=sender_email, sender_name=sender_name,
                        reply_to_email=reply_to_email)

    api_key = os.getenv("MAILCHIMP_API_KEY", "").strip()
    server = os.getenv("MAILCHIMP_SERVER", "").strip()
    audience_id = os.getenv("MAILCHIMP_AUDIENCE_ID", "").strip()

    if not server and "-" in api_key:
        server = api_key.rsplit("-", 1)[-1].strip()

    if require:
        missing = [
            name for name, val in (
                ("MAILCHIMP_API_KEY", api_key),
                ("MAILCHIMP_SERVER", server),
                ("MAILCHIMP_AUDIENCE_ID", audience_id),
            ) if not val
        ]
        if missing:
            raise RuntimeError(
                f"Missing credential(s) in {client.env_file}: {', '.join(missing)}"
            )

    return Settings(api_key=api_key, server=server, audience_id=audience_id)


def list_clients() -> list[str]:
    if not CLIENTS_DIR.exists():
        return []
    return sorted(p.name for p in CLIENTS_DIR.iterdir()
                  if (p / "client.json").exists())
