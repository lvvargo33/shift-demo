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
import re
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


# --- reporting layer helpers (workbook-aligned; see DASHBOARD_REVAMP_SPEC) ----
# These power the Dashboard tiles / Funnel / Insights only. They do NOT feed the
# Recovery Queue eligibility engine, which keeps its own trial/conversion logic.

# One line item inside the transactions `items` column, e.g.
#   "1 2 Week Trial ($45.00) $45.00; 1 Chalk Bag ($12.00) $12.00"
_LINE_RE = re.compile(r"^\s*(\d+)\s+(.+?)\s*\(\$([0-9.]+)\)")


def _norm_date(s: str) -> str | None:
    """Best-effort -> YYYY-MM-DD. Handles ISO-ish, tz-suffixed, and m/d/yyyy."""
    if not s:
        return None
    s = re.sub(r"\s+[A-Z]{2,4}\s*$", "", str(s).strip())  # drop trailing 'EDT'
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        mo, da, yr = m.group(1), m.group(2), m.group(3)
        return f"{yr}-{int(mo):02d}-{int(da):02d}"
    return None


def categorize_product(name: str, rules: list[dict], default: str) -> str:
    """Map one product-name string to a category via ordered, first-match-wins
    substring rules from client.json reporting.product_rules. A rule matches on
    'any' (any substring present) or 'all' (every substring present). Ported
    verbatim from the workbook's categorize_product()."""
    n = (name or "").lower()
    if not n:
        return default
    for rule in rules:
        anys = rule.get("any")
        if anys and any(s in n for s in anys):
            return rule["category"]
        alls = rule.get("all")
        if alls and all(s in n for s in alls):
            return rule["category"]
    return default


def _daypass_line_total(items: str, daypass_skus: list[str]) -> float:
    """Sum of (qty x unit price) across the day-pass line items in one
    transaction's items string. 0.0 when no day-pass line is present."""
    total = 0.0
    for part in (items or "").split(";"):
        m = _LINE_RE.match(part)
        if not m:
            continue
        name = m.group(2).lower()
        if any(sku in name for sku in daypass_skus):
            try:
                total += int(m.group(1)) * float(m.group(3))
            except ValueError:
                continue
    return total


def _ftv_category_for_tx(items: str, rules: list[dict], default: str,
                         qualifying: set[str]) -> str | None:
    """Category of the first FTV-qualifying line item in this transaction's
    items string (in listed order), or None if the tx has no qualifying item.
    Mirrors the workbook taking the first qualifying line item per earliest tx."""
    for part in (items or "").split(";"):
        m = _LINE_RE.match(part)
        if not m:
            continue
        cat = categorize_product(m.group(2).strip(), rules, default)
        if cat in qualifying:
            return cat
    return None


@dataclass
class Climber:
    climber_id: str
    name: str = ""
    email: str = ""
    trial_date: str | None = None      # earliest SUCCEEDED trial purchase
    visit_days: set[str] = field(default_factory=set)
    daypass_dates: list[str] = field(default_factory=list)  # day-pass/punch buy dates
    # Day-pass buys discounted by >= half the day-pass line price. Beta's export
    # never names the coupon used, so this is the footprint of the 50%-off
    # SECONDVISIT offer code (feeds the Insights "Offer email results" slide).
    discounted_daypass_dates: list[str] = field(default_factory=list)
    is_member: bool = False            # appears in Memberships.csv
    is_converted: bool = False         # bought a conversion_sku OR is_member
    conversion_date: str | None = None # earliest conversion purchase date

    # --- reporting layer (workbook-aligned; not used by the queue engine) ---
    ftv_time: str | None = None        # sortable key of earliest FTV-qualifying tx
    ftv_category: str | None = None    # entry product of that FTV tx (DAY_PASS, TRIAL_2WK, ...)
    is_staff: bool = False             # founder/staff email -> excluded from FTV cohort
    reporting_converted: bool = False  # has a real Memberships row (workbook 'converted_to_member')
    membership_created: str | None = None  # earliest membership start date (for timing)

    @property
    def ftv_date(self) -> str | None:
        return self.ftv_time[:10] if self.ftv_time else None

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
    def second_visit_date(self) -> str | None:
        """Date of the 2nd distinct check-in day (the second_visit anchor).
        None until the climber has actually returned, so second_visit-anchored
        triggers fail closed for one-visit climbers."""
        if len(self.visit_days) < 2:
            return None
        return sorted(self.visit_days)[1]

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

    # reporting-layer config (workbook-aligned); empty -> reporting fields stay blank
    rep = client.reporting or {}
    rep_rules = rep.get("product_rules", [])
    rep_default = rep.get("product_default_category", "RETAIL")
    rep_qualifying = set(rep.get("ftv_qualifying_categories", []))
    staff_tier_kw = [k.lower() for k in rep.get("staff_tier_keywords", [])]
    staff_emails: set[str] = {e.strip().lower() for e in rep.get("staff_emails", []) if e}

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
                # Promo-redemption footprint: the whole-transaction discount
                # covers at least half the day-pass line total. (Coupon names
                # never appear in the export, so amount is the only signal.)
                try:
                    disc = float(row.get("discount_total") or 0)
                except ValueError:
                    disc = 0.0
                if disc > 0:
                    dp_total = _daypass_line_total(items, daypass_skus)
                    if dp_total > 0 and disc >= 0.5 * dp_total:
                        c.discounted_daypass_dates.append(d)
            if client.conversion_skus and _matches(items, client.conversion_skus):
                c.is_converted = True
                if c.conversion_date is None or (d and d < c.conversion_date):
                    c.conversion_date = d

            # reporting: earliest FTV-qualifying transaction sets entry product.
            if rep_qualifying:
                cat = _ftv_category_for_tx(items, rep_rules, rep_default, rep_qualifying)
                if cat:
                    tk = (row.get("time") or "")[:19]  # full-timestamp sort key
                    if c.ftv_time is None or tk < c.ftv_time:
                        c.ftv_time = tk
                        c.ftv_category = cat

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
            prices = (row.get("prices") or "").strip()
            meta = (row.get("metadata") or "")
            mem_email = (row.get("email") or "").strip().lower()
            # staff detection (workbook: prices contains 'Staff' or metadata 'STAFFSHIFT')
            blob = (prices + " " + meta).lower()
            if staff_tier_kw and mem_email and any(k in blob for k in staff_tier_kw):
                staff_emails.add(mem_email)
            if cid:
                c = get(cid)
                c.is_member = True
                c.is_converted = True
                # reporting conversion = a real membership row (non-empty price/tier)
                if prices:
                    c.reporting_converted = True
                    created = _norm_date(row.get("created_date", ""))
                    if created and (c.membership_created is None
                                    or created < c.membership_created):
                        c.membership_created = created

    # reporting: flag staff/founder climbers (excluded from the FTV cohort)
    if staff_emails:
        for c in climbers.values():
            if (c.email or "").strip().lower() in staff_emails:
                c.is_staff = True

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
