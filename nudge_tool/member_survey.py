"""Who gets a membership survey email today.

Build step 5 (Membership Surveys SPEC). The recurring member-survey queue, kept
deliberately separate from `engine.build_queue`.

WHY THIS IS NOT A NORMAL TRIGGER
Every trigger in triggers.py is VISIT-ANCHORED: it resolves an anchor
(first_visit, second_visit, last_daypass, ...) to a date on a Climber built from
check-in and transaction data, then asks whether today falls in a day-window
around it. The member survey has none of that shape. It goes to every active
member on a rolling cadence regardless of whether they have visited, and its
audience comes from member_status.csv rather than from the visit data. Forcing
it into the trigger model would have meant a fake anchor and a Climber record
for members who never appear in the visit files at all.

THE OWN-GATE RULE
`enabled` is this module's own switch, separate from NUDGE_LIVE_ENABLED and from
the client's live_enabled. Turning FTV sending on must NOT start mailing 187
members a survey. Both gates have to be open, and this one defaults to False, so
a missing or malformed config block sends nobody.

CADENCE
One send per member per `cooldown_days` (180). At `daily_cap` (10) a wave of 187
takes about 19 days. No reminders and one send per wave were locked with Luke on
2026-08-19.

Ordering is deterministic. A re-run on the same day must pick the same people,
or a crash-and-retry would mail a different slice and quietly double the day's
volume. Longest-waiting first, then oldest previous send, then member id.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import audience

# The stable key this module writes into the outreach log. Cooldown is looked up
# by (email, trigger_name), so renaming this would reset every member's clock
# and re-survey the whole base. Treat it as permanent.
TRIGGER_NAME = "member_survey"
DEFAULT_TAG = "send_it_member_survey"
DEFAULT_TEMPLATE = "member_survey"


@dataclass(frozen=True)
class Config:
    """The `member_survey` block of client.json."""
    enabled: bool = False        # own gate; default OFF on purpose
    daily_cap: int = 10
    cooldown_days: int = 180
    survey_url: str = ""
    template_id: str = DEFAULT_TEMPLATE
    tag: str = DEFAULT_TAG
    # Chris's POSITIVE_THRESHOLD (Lovable response-templates.ts). The positive
    # reply goes out only when EVERY selection metric is >= this; otherwise the
    # lowest metric picks the follow-up draft. Luke chose this rule 2026-08-21.
    positive_threshold: int = 9

    @property
    def configured(self) -> bool:
        """Enough config present to send at all. A blank survey_url would mail a
        link to nowhere, so it counts as unconfigured rather than defaulting."""
        return bool(self.survey_url and self.template_id and self.tag)


@dataclass
class Skips:
    """Why members were not queued today. Surfaced in the report + the UI."""
    not_enabled: int = 0        # the gate is closed (whole audience)
    suppressed: int = 0         # Brevo opt-out / blocked
    in_cooldown: int = 0        # surveyed within cooldown_days
    over_cap: int = 0           # eligible today but past daily_cap
    reasons: list = field(default_factory=list)   # human notes for the report


@dataclass(frozen=True)
class QueueEntry:
    member: audience.Member
    link: str                   # the member's personal ?mid= survey link
    last_sent: str = ""         # previous member_survey send, "" if never

    @property
    def first_time(self) -> bool:
        return not self.last_sent


def load_config(client) -> Config:
    """Read the `member_survey` block off a ClientConfig.

    Every field falls back to the module default, and `enabled` falls back to
    False, so a client with no block at all is simply off."""
    raw = getattr(client, "member_survey", None)
    if not isinstance(raw, dict):
        return Config()
    return Config(
        enabled=bool(raw.get("enabled", False)),
        daily_cap=int(raw.get("daily_cap", 10) or 0),
        cooldown_days=int(raw.get("cooldown_days", 180) or 0),
        survey_url=str(raw.get("survey_url", "") or "").strip(),
        template_id=str(raw.get("template_id", DEFAULT_TEMPLATE) or "").strip(),
        tag=str(raw.get("tag", DEFAULT_TAG) or "").strip(),
        positive_threshold=int(raw.get("positive_threshold", 9) or 9),
    )


def _days_between(earlier: str, later: str) -> int:
    """Whole days between two YYYY-MM-DD strings. Returns a huge number if the
    earlier date is unparseable, so an unreadable log entry is treated as
    'long ago' and never blocks a send forever."""
    from datetime import date
    try:
        a = date.fromisoformat(earlier[:10])
        b = date.fromisoformat(later[:10])
    except (ValueError, TypeError):
        return 10 ** 6
    return (b - a).days


def _order_key(entry: QueueEntry) -> tuple:
    """Longest-waiting first: never-surveyed members ahead of everyone, then
    oldest previous send. member_id breaks ties so the order is identical on
    every run (see the module docstring on re-run safety)."""
    return (0 if entry.first_time else 1,
            entry.last_sent or "",
            entry.member.member_id)


def build_queue(members, log, cfg: Config, today: str,
                suppressed: set | None = None) -> tuple[list[QueueEntry], Skips]:
    """Today's member-survey send list.

    `members`  -- sendable Members from audience.select (already deduped by
                  email and stripped of doNotMail)
    `log`      -- outreach.OutreachLog, for the cooldown
    `today`    -- YYYY-MM-DD
    `suppressed` -- lowercased emails Brevo will not accept (opt-outs/blocks)

    Returns (queue, skips). The queue is at most cfg.daily_cap long.
    """
    skips = Skips()
    members = list(members)

    if not cfg.enabled:
        skips.not_enabled = len(members)
        skips.reasons.append("member_survey.enabled is false (own gate, "
                             "separate from NUDGE_LIVE_ENABLED)")
        return [], skips

    if not cfg.configured:
        skips.not_enabled = len(members)
        missing = [n for n, v in (("survey_url", cfg.survey_url),
                                  ("template_id", cfg.template_id),
                                  ("tag", cfg.tag)) if not v]
        skips.reasons.append(f"member_survey config incomplete: missing "
                             f"{', '.join(missing)}")
        return [], skips

    if cfg.daily_cap <= 0:
        skips.not_enabled = len(members)
        skips.reasons.append(f"member_survey.daily_cap is {cfg.daily_cap}")
        return [], skips

    supp = {e.strip().lower() for e in (suppressed or set())}
    eligible: list[QueueEntry] = []

    for m in members:
        if m.email in supp:
            skips.suppressed += 1
            continue

        last = log.last_sent(m.email, TRIGGER_NAME) or ""
        if last and _days_between(last, today) < cfg.cooldown_days:
            skips.in_cooldown += 1
            continue

        eligible.append(QueueEntry(
            member=m,
            link=audience.survey_link(cfg.survey_url, m.member_id),
            last_sent=last,
        ))

    eligible.sort(key=_order_key)
    queue = eligible[:cfg.daily_cap]
    skips.over_cap = len(eligible) - len(queue)
    return queue, skips


def wave_summary(queue, skips: Skips, total_sendable: int, cfg: Config) -> list[str]:
    """Report-email lines describing today's slice and the wave behind it."""
    if not cfg.enabled:
        return ["  member surveys: OFF (member_survey.enabled=false)"]
    remaining = skips.over_cap
    out = [f"  member surveys: {len(queue)} queued today "
           f"(cap {cfg.daily_cap}, {total_sendable} sendable)"]
    if remaining:
        days = -(-remaining // cfg.daily_cap) if cfg.daily_cap else 0
        out.append(f"    {remaining} still waiting -> about {days} more day(s)")
    if skips.in_cooldown:
        out.append(f"    {skips.in_cooldown} in cooldown "
                   f"(surveyed within {cfg.cooldown_days} days)")
    if skips.suppressed:
        out.append(f"    {skips.suppressed} suppressed (opted out / blocked)")
    return out
