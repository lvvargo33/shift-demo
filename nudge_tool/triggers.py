"""Trigger definitions live in each client's client.json, not in code.

This module gives them a typed shape. Section 13 shipped ONE trigger (day-5
trial); section 14 (FTV_PILOT_SPEC.md) generalizes the engine to the whole
message catalog. A trigger now has:

  anchor  -- which event the day-window is measured from:
              first_visit | trial_purchase | survey_sent | last_visit | last_daypass
  days_since_min/max -- the inclusive window around that anchor (in days)
  requires -- a dict of extra conditions ALL of which must hold. Recognized keys:
              no_return       (bool)  visit_count <= 1
              max_visits      (int)   visit_count <= n
              min_visits      (int)   visit_count >= n
              daypass_repeat  (bool)  >= daypass_repeat.min_count day-pass buys
                                      within daypass_repeat.window_days (config)
              survey_blocker  (str)   survey Q3 answer == value   (needs S2 data)
              survey_intent   (str)   survey intent bucket == value (needs S2 data)
  once_only / cooldown_days -- repeat protection, enforced via the local
              outreach log (wired in S3); structural here.

Add a nudge = add a block here + a templates/<template_id>.md file. Change
timing = edit anchor / days_since_min/max. Change who qualifies = edit requires.
No Python edits.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Anchors the engine knows how to resolve to a date on a Climber.
ANCHORS = {"first_visit", "trial_purchase", "survey_sent", "last_visit", "last_daypass"}


@dataclass(frozen=True)
class Trigger:
    name: str                       # human key, e.g. "comeback_soft"
    tag: str                        # Mailchimp tag the engine applies
    template_id: str                # which templates/<id>.md to preview
    anchor: str                     # one of ANCHORS
    days_since_min: int             # window lower bound (days since anchor event)
    days_since_max: int             # window upper bound (inclusive)
    description: str = ""
    requires: dict = field(default_factory=dict)  # extra conditions (see module docstring)
    once_only: bool = False         # never repeat for a person (needs outreach log, S3)
    cooldown_days: int = 0          # else honor this gap between repeats (S3)
    active: bool = True
    group: str = ""                 # campaign group: sending ANY trigger in this
                                    # group suppresses every other trigger in it for
                                    # the same person (e.g. the "second_visit" offer
                                    # set, so a climber gets exactly one offer email
                                    # whether it's the personalized or generic one).


def trigger_from_dict(d: dict) -> Trigger:
    anchor = d.get("anchor", "trial_purchase")
    if anchor not in ANCHORS:
        raise ValueError(
            f"trigger '{d.get('name')}' has unknown anchor '{anchor}'. "
            f"Known: {sorted(ANCHORS)}"
        )
    return Trigger(
        name=d["name"],
        tag=d["tag"],
        template_id=d["template_id"],
        anchor=anchor,
        days_since_min=int(d["days_since_min"]),
        days_since_max=int(d["days_since_max"]),
        description=d.get("description", ""),
        requires=dict(d.get("requires", {})),
        once_only=bool(d.get("once_only", False)),
        cooldown_days=int(d.get("cooldown_days", 0)),
        active=bool(d.get("active", True)),
        group=d.get("group", ""),
    )


def triggers_from_config(rows: list[dict]) -> list[Trigger]:
    return [trigger_from_dict(r) for r in rows]
