"""S5 test-contact mode: prove the write path without touching a real climber.

`--test` computes the normal queue, then REMAPS it onto plus-addressed dummy
contacts derived from `test_contacts.base_email` in client.json. One dummy per
distinct tag in today's queue (capped at `max`), e.g.

    base = lvvargo33+shifttest@gmail.com
    tag  = survey_request
    ->     lvvargo33+shifttest_survey_request@gmail.com   (delivers to base's inbox)

Why this is safe:
  - The tool NEVER tags a real climber email in test mode. It only ever tags
    the derived dummies.
  - `assert_all_dummy()` independently re-checks every target before any write
    and raises (fail closed) if even one is not a plus-variant of base_email,
    so a real address cannot slip through even if derivation had a bug.
  - All dummies share base_email's real mailbox (Gmail plus-addressing), so Luke
    actually sees the tag/email land while the audience only gains throwaway
    contacts that `cleanup` removes.

The module also closes the once-only / cooldown loop (precedence rule 4) on the
DUMMY emails using the same `outreach.OutreachLog`, so a second `--test` run
correctly reports "already sent, suppressed" and tags nobody new. Pure logic +
string work here: no network, no file writes (cli.py owns those).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from . import outreach
from .config import ClientConfig


@dataclass
class TestTarget:
    climber_id: str
    real_name: str          # the real climber this row stands in for (display only)
    dummy_email: str        # the plus-addressed throwaway we actually tag
    tag: str
    template_id: str
    trigger_name: str
    anchor_date: str = ""
    days_since: int = 0


class TestSafetyError(RuntimeError):
    """Raised when a target is not a safe plus-addressed dummy. Fail closed."""


def _split(email: str) -> tuple[str, str]:
    local, _, domain = email.strip().lower().partition("@")
    return local, domain


def dummy_for(base_email: str, tag: str) -> str:
    """Derive the dummy address for one tag off the base. Appends `_<tag>` to the
    base's local part so every tag gets a distinct contact that still delivers to
    base_email's mailbox via plus-addressing."""
    local, domain = _split(base_email)
    return f"{local}_{tag}@{domain}"


def derive_targets(queue, client: ClientConfig) -> list[TestTarget]:
    """One dummy target per distinct tag in the queue (queue priority order),
    capped at test_contacts.max. Empty if no base_email is configured."""
    base = (client.test_contacts.get("base_email") or "").strip()
    if not base:
        return []
    max_n = int(client.test_contacts.get("max", 8) or 8)
    seen: set[str] = set()
    targets: list[TestTarget] = []
    for q in queue:
        if q.tag in seen:
            continue
        seen.add(q.tag)
        targets.append(TestTarget(
            climber_id=q.climber_id,
            real_name=q.name,
            dummy_email=dummy_for(base, q.tag),
            tag=q.tag,
            template_id=q.template_id,
            trigger_name=q.trigger_name,
            anchor_date=q.anchor_date,
            days_since=q.days_since,
        ))
        if len(targets) >= max_n:
            break
    return targets


def assert_all_dummy(targets: list[TestTarget], base_email: str) -> None:
    """Independent safety gate: every target must be a plus-variant of base_email.
    Raises TestSafetyError on the first violation so the run writes nothing."""
    base = (base_email or "").strip().lower()
    if "+" not in base:
        raise TestSafetyError(
            f"test_contacts.base_email ({base_email!r}) has no '+'. It must be a "
            "plus-addressable address (e.g. you+shifttest@gmail.com) so test mail "
            "stays on your own inbox.")
    base_local, base_domain = _split(base)
    for t in targets:
        email = (t.dummy_email or "").strip().lower()
        local, domain = _split(email)
        ok = ("+" in email
              and domain == base_domain
              and local.startswith(base_local + "_"))
        if not ok:
            raise TestSafetyError(
                f"refusing to tag {t.dummy_email!r}: not a plus-addressed dummy of "
                f"{base_email!r}. Test mode never tags a non-dummy address.")


def _within_cooldown(last_sent: str, asof: date, cooldown_days: int) -> bool:
    try:
        d = date.fromisoformat(last_sent[:10])
    except (ValueError, TypeError):
        return False
    return (asof - d).days < cooldown_days


def filter_already_sent(targets: list[TestTarget], log: outreach.OutreachLog,
                        client: ClientConfig, asof: date):
    """Apply once-only / cooldown on the DUMMY emails (precedence rule 4) so a
    repeat --test run suppresses instead of re-tagging. Returns (to_send,
    suppressed) where suppressed items are (target, reason)."""
    by_tag = {t.tag: t for t in client.triggers}
    to_send: list[TestTarget] = []
    suppressed: list[tuple[TestTarget, str]] = []
    for t in targets:
        trig = by_tag.get(t.tag)
        dates = log.sent_dates(t.dummy_email, t.tag)
        if dates and trig and trig.once_only:
            suppressed.append((t, "already_sent"))
        elif dates and trig and getattr(trig, "cooldown_days", 0) and \
                _within_cooldown(max(dates), asof, int(trig.cooldown_days)):
            suppressed.append((t, "cooldown"))
        else:
            to_send.append(t)
    return to_send, suppressed


def log_rows(sent: list[TestTarget], asof: date) -> list[dict]:
    """Outreach-log rows for dummies we actually tagged (mode=test). Records the
    DUMMY email (the real address we wrote to), so re-runs suppress correctly and
    no real climber email enters the log from a test."""
    d = asof.isoformat()
    return [{
        "sent_date": d,
        "email": t.dummy_email,
        "climber_id": t.climber_id,
        "trigger_name": t.trigger_name,
        "tag": t.tag,
        "mode": "test",
    } for t in sent]
