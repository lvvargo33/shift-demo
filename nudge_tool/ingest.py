"""Read Beta exports into climber records the engine can reason over.

Sources (paths in client.json):
  Transactions.csv  -> trial buys, day-pass buys, conversions (climber_id)
  sessions ...csv   -> check-ins; distinct entry-days = visits (climber_key)
  Memberships.csv   -> who is / was a member (climber_key)

Join: climber_id (transactions) == climber_key (sessions, memberships). Verified
clean numeric in the tracker work (958/991 trial buyers present in sessions).

Product tiers (section 14.5) make commitment client-defined, all by substring
match on the transaction `items` text, lowercased:
  trial_skus           -> bought a trial            -> trial_date (anchor)
  low_commitment_skus  -> day pass / punch / trial  -> daypass_dates (minus trial)
  conversion_skus      -> became a member           -> is_converted + conversion_date
A Memberships row also marks is_member (a second, independent conversion signal).

This module is pure read + shape: no network, no eligibility logic (engine.py),
so it stays trivially testable and the trigger rules live in one place.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date

from .config import ClientConfig

csv.field_size_limit(10_000_000)  # transactions rows are wide


def _matches(items: str, skus: list[str]) -> bool:
    s = items.lower()
    return any(sku in s for sku in skus)


def _d(s: str) -> str:
    """First 10 chars of an ISO-ish timestamp -> YYYY-MM-DD (or '')."""
    return (s or "")[:10]


@dataclass
class Climber:
    climber_id: str
    name: str = ""
    email: str = ""
    trial_date: str | None = None      # earliest SUCCEEDED trial purchase
    visit_days: set[str] = field(default_factory=set)
    daypass_dates: list[str] = field(default_factory=list)  # day-pass/punch buy dates
    is_member: bool = False            # appears in Memberships.csv
    is_converted: bool = False         # bought a conversion_sku OR is_member
    conversion_date: str | None = None # earliest conversion purchase date

    @property
    def visit_count(self) -> int:
        return len(self.visit_days)

    @property
    def first_visit_date(self) -> str | None:
        return min(self.visit_days) if self.visit_days else None

    @property
    def last_visit_date(self) -> str | None:
        return max(self.visit_days) if self.visit_days else None

    @property
    def last_daypass_date(self) -> str | None:
        return max(self.daypass_dates) if self.daypass_dates else None

    # S2 (survey loop) fills these; absent until a response is matched, so
    # survey-anchored triggers and survey_blocker requirements match nobody yet.
    survey_sent_date: str | None = None   # response timestamp (survey_sent anchor)
    survey_blocker: str | None = None     # Q3 -> blocker value
    survey_intent: str | None = None      # Q2 -> intent bucket
    survey_answered: bool = False         # has a matched survey response
    safety_flag: bool = False             # Q4 free text tripped a safety keyword

    def daypass_count_within(self, asof: date, window_days: int) -> int:
        """Day-pass purchases in the [asof - window_days, asof] window."""
        lo = _add_days(asof, -window_days)
        return sum(1 for d in self.daypass_dates if lo <= d <= asof.isoformat())


@dataclass
class Dataset:
    climbers: dict[str, Climber]
    tx_max_date: str | None
    sessions_max_date: str | None
    trial_buyer_count: int


def load(client: ClientConfig) -> Dataset:
    climbers: dict[str, Climber] = {}
    # day-pass = low_commitment minus the trial skus (a trial isn't a day pass)
    daypass_skus = [s for s in client.low_commitment_skus
                    if s not in client.trial_skus]

    def get(cid: str) -> Climber:
        c = climbers.get(cid)
        if c is None:
            c = Climber(climber_id=cid)
            climbers[cid] = c
        return c

    # --- Transactions: trials, day passes, conversions ---
    tx_dates: list[str] = []
    with open(client.transactions_csv, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            d = _d(row.get("time", ""))
            if d:
                tx_dates.append(d)
            if row.get("state") != "SUCCEEDED":
                continue
            items = row.get("items") or ""
            cid = (row.get("climber_id") or "").strip()
            if not cid:
                continue
            c = get(cid)
            c.name = c.name or (row.get("climber_name") or "").strip()
            c.email = c.email or (row.get("climber_email") or "").strip()

            if _matches(items, client.trial_skus):
                if c.trial_date is None or (d and d < c.trial_date):
                    c.trial_date = d
            if daypass_skus and _matches(items, daypass_skus) and d:
                c.daypass_dates.append(d)
            if client.conversion_skus and _matches(items, client.conversion_skus):
                c.is_converted = True
                if c.conversion_date is None or (d and d < c.conversion_date):
                    c.conversion_date = d

    trial_buyers = sum(1 for c in climbers.values() if c.trial_date)

    # --- Sessions: distinct entry-days per climber ---
    ses_dates: list[str] = []
    with open(client.sessions_csv, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            cid = (row.get("climber_key") or "").strip()
            e = _d(row.get("entry", ""))
            if not (cid and e):
                continue
            ses_dates.append(e)
            c = get(cid)
            c.visit_days.add(e)
            if not c.name:
                c.name = (row.get("climber") or "").strip()
            if not c.email:
                c.email = (row.get("email") or "").strip()

    # --- Memberships: a second conversion signal ---
    with open(client.memberships_csv, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            cid = (row.get("climber_key") or "").strip()
            if cid:
                c = get(cid)
                c.is_member = True
                c.is_converted = True

    for c in climbers.values():
        c.daypass_dates.sort()

    return Dataset(
        climbers=climbers,
        tx_max_date=max(tx_dates) if tx_dates else None,
        sessions_max_date=max(ses_dates) if ses_dates else None,
        trial_buyer_count=trial_buyers,
    )


def first_name(full: str) -> str:
    return (full or "").strip().split(" ")[0] if full else ""


def days_between(start: str, asof: date) -> int:
    return (asof - date.fromisoformat(start)).days


def _add_days(d: date, n: int) -> str:
    from datetime import timedelta
    return (d + timedelta(days=n)).isoformat()
