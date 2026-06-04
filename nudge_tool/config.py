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


@dataclass(frozen=True)
class Settings:
    api_key: str
    server: str
    audience_id: str

    @property
    def api_base(self) -> str:
        return f"https://{self.server}.api.mailchimp.com/3.0"


@dataclass
class ClientConfig:
    slug: str
    client_name: str
    live_enabled: bool
    env_file: Path
    transactions_csv: Path
    memberships_csv: Path
    sessions_csv: Path
    trial_skus: list[str]
    low_commitment_skus: list[str]
    conversion_skus: list[str]
    daypass_repeat: dict          # {"min_count": int, "window_days": int}
    reporting: dict               # workbook-aligned reporting defs (cohort/entry-product/staff/conversion)
    survey: dict                  # survey loop config (S2); {} or {"enabled": False} = off
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
        live_enabled=bool(cfg.get("live_enabled", False)),
        env_file=_resolve(cfg.get("env_file", ".env")),
        transactions_csv=_resolve(data["transactions_csv"]),
        memberships_csv=_resolve(data["memberships_csv"]),
        sessions_csv=_resolve(data["sessions_csv"]),
        trial_skus=[s.lower() for s in cfg.get("trial_skus", [])],
        low_commitment_skus=[s.lower() for s in cfg.get("low_commitment_skus", [])],
        conversion_skus=[s.lower() for s in cfg.get("conversion_skus", [])],
        daypass_repeat=dict(cfg.get("daypass_repeat", {"min_count": 2, "window_days": 30})),
        reporting=dict(cfg.get("reporting", {})),
        survey=dict(cfg.get("survey", {})),
        links=dict(cfg.get("links", {})),
        test_contacts=dict(cfg.get("test_contacts", {})),
        triggers=triggers_from_config(cfg.get("triggers", [])),
        templates=load_templates(client_dir / "templates"),
        client_dir=client_dir,
    )


def load_settings(client: ClientConfig, require: bool = True) -> Settings:
    """Load this client's Mailchimp credentials from their env_file."""
    load_dotenv(client.env_file, override=True)
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
