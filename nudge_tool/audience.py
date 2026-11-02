"""Who gets a membership survey, and who is deliberately left out.

Build step 4 (Membership Surveys SPEC). Turns raw customer rows into a sendable
list plus the exclusion counts the UI is REQUIRED to show.

The audience rule (verified live against facility APN 2026-08-19):

    active member = customerType == "MEMBER" AND currentStatus == "OK"

`memberships.csv` cannot answer this: it has no status column. The status lives
only in `customers_pii.csv`, which `rgp_pull.py` already produces.

THE FUNNEL, in this order (the order is load-bearing, see below):

    active members (MEMBER + OK)
      minus rows with no usable email
      minus doNotMail
      minus duplicate email addresses (households)
      = sendable

Two rules Luke locked on 2026-08-19 that this module exists to enforce:

1. **doNotMail members leave EVERY denominator**, not just the send list. They
   are not counted in response rate, NPS base, dimension averages, or wave
   coverage. A silent exclusion inflates the response rate and flatters the
   sample, so `Counts.ui_sentence()` states the exclusion out loud and the UI is
   expected to render it. Show the COUNT of excluded members and nothing else
   about them.

2. **One send per shared email address.** ABC sells family plans up to 6, so
   several members can share one inbox. The household is surveyed once and the
   response is attributed to the ADDRESS, not to whichever member won the tie.

Ordering matters because the counts are reported as a funnel: each number is
"how many were removed at this step", so moving a step changes what the numbers
mean. Email-before-doNotMail matches the funnel recorded in the SPEC.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

# customerType / currentStatus values that define an active member. Compared
# case-insensitively after stripping, because RGP has shipped both cases.
ACTIVE_TYPE = "member"
ACTIVE_STATUS = "ok"

# doNotMail arrives as "0"/"1" in today's pull. Accept the obvious truthy
# spellings too rather than trusting one representation: a wrong read here mails
# somebody who asked not to be mailed, so anything unrecognized is treated as
# DO NOT MAIL (fail closed toward not sending).
_FALSEY = {"", "0", "false", "no", "n", "none", "null"}


@dataclass(frozen=True)
class Member:
    """One active member, normalized. `member_id` is the customerGuid, which is
    also the join key for every canonical CSV (climber_id in transactions,
    climber_key in sessions/memberships) and the value that rides the survey
    link as `?mid=`. It is a 36-char UUID, so it is URL-safe as-is."""
    member_id: str
    first_name: str
    last_name: str
    email: str            # lowercased + stripped; this is the dedupe key
    membership_start: str
    membership_exp: str

    @property
    def name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p)


@dataclass
class Counts:
    """Every number the UI needs to describe the audience honestly."""
    member_rows: int = 0              # every customerType == MEMBER, any status
    inactive_by_status: dict = field(default_factory=dict)  # FROZEN/TERMINATED/...
    active: int = 0                   # MEMBER + OK
    no_email: int = 0                 # dropped: blank or malformed address
    do_not_mail: int = 0              # dropped AT THAT STEP (had an email too)
    do_not_mail_flagged: int = 0      # ALL active members who asked not to be
                                      # mailed, including ones already dropped
                                      # for having no address. This is the
                                      # number the disclosure quotes; see
                                      # ui_sentence().
    shared_email_folded: int = 0      # dropped: another member has this address
    sendable: int = 0

    @property
    def households(self) -> int:
        """Members who share an address with someone already being sent to."""
        return self.shared_email_folded

    def ui_sentence(self) -> str:
        """The required disclosure. Luke, 2026-08-19: make the exclusion visible.

        Rendered near the top of every membership-survey view.

        Quotes `do_not_mail_flagged`, NOT the funnel's `do_not_mail`. The two
        differ because members with no email address are dropped one step
        earlier, so the funnel number silently undercounts how many people
        actually asked not to be mailed. The disclosure is a statement about
        respecting a request, so it uses the full flagged count. The funnel
        keeps the step count, because there the numbers have to add up."""
        s = f"{self.sendable} members surveyed."
        if self.do_not_mail_flagged:
            s += (f" {self.do_not_mail_flagged} excluded at their request "
                  f"(do not mail) and not counted in any number on this page.")
        return s

    def funnel_lines(self) -> list[str]:
        """Human-readable funnel for the report email and the settings view."""
        out = [f"  active members (MEMBER + OK)   {self.active}"]
        if self.no_email:
            out.append(f"    minus no usable email        -{self.no_email}")
        if self.do_not_mail:
            out.append(f"    minus doNotMail              -{self.do_not_mail}")
        if self.shared_email_folded:
            out.append(f"    minus shared addresses       -{self.shared_email_folded}")
        out.append(f"  sendable                       {self.sendable}")
        return out


def _truthy(v) -> bool:
    return str(v).strip().lower() not in _FALSEY


def _usable_email(v) -> str:
    """Normalized address, or "" if it cannot be mailed. Lowercased because the
    dedupe key must treat Chris@x.com and chris@x.com as one household."""
    e = str(v or "").strip().lower()
    if not e or "@" not in e:
        return ""
    local, _, domain = e.rpartition("@")
    if not local or "." not in domain:
        return ""
    return e


# RGP writes its null date as "0000-00-00", not as an empty string. Treated as
# missing: without this, a member with NO start date sorts before every real
# date and wins the household tie-break, which is backwards. Caught 2026-08-21
# when the first real member_status.csv came out full of 0000-00-00.
_NULL_DATES = {"", "0000-00-00", "0000-00-00 00:00:00", "None", "null"}


def _clean_date(v) -> str:
    s = str(v or "").strip()
    return "" if s in _NULL_DATES else s


def _sort_key(m: Member) -> tuple:
    """Stable winner when several members share an address.

    Longest-standing member represents the household (earliest membership start),
    with the guid as a deterministic tie-break. Stability is the point: the same
    person must win on every run, or a household would get re-surveyed under a
    different member_id and the 180-day cooldown would not hold. Missing start
    dates sort last so a member with a real date always beats one without."""
    return (m.membership_start or "9999-12-31", m.member_id)


def select(rows) -> tuple[list[Member], Counts]:
    """Rows from member_status.csv -> (sendable members, counts).

    Expects that file's CANONICAL column names (climber_key, first_name,
    last_name, email, status, do_not_mail, membership_start, membership_exp),
    which is what `rgp_ingest.build_member_status` writes and what the Drive
    round-trip delivers to the cloud. Use `from_customers_pii()` to feed raw
    customer rows instead.

    No customerType check: member_status.csv is MEMBER-only by construction, so
    every row is a member of some status. Takes any iterable of mappings so the
    caller decides where the data came from without this module caring.
    """
    counts = Counts()
    status_tally: Counter = Counter()
    active: list[Member] = []

    for r in rows:
        counts.member_rows += 1

        status = str(r.get("status") or "").strip()
        if status.lower() != ACTIVE_STATUS:
            status_tally[status or "(blank)"] += 1
            continue
        counts.active += 1

        dnm = _truthy(r.get("do_not_mail"))
        if dnm:
            counts.do_not_mail_flagged += 1

        email = _usable_email(r.get("email"))
        if not email:
            counts.no_email += 1
            continue

        if dnm:
            counts.do_not_mail += 1
            continue

        active.append(Member(
            member_id=str(r.get("climber_key") or "").strip(),
            first_name=str(r.get("first_name") or "").strip(),
            last_name=str(r.get("last_name") or "").strip(),
            email=email,
            membership_start=_clean_date(r.get("membership_start")),
            membership_exp=_clean_date(r.get("membership_exp")),
        ))

    counts.inactive_by_status = dict(status_tally.most_common())

    # One send per address: sort first so the winner is deterministic, then keep
    # the first sighting of each address.
    seen: set[str] = set()
    sendable: list[Member] = []
    for m in sorted(active, key=_sort_key):
        if m.email in seen:
            counts.shared_email_folded += 1
            continue
        seen.add(m.email)
        sendable.append(m)

    counts.sendable = len(sendable)
    return sendable, counts


def from_customers_pii(rows) -> tuple[list[Member], Counts]:
    """Same as select(), but for RAW customers_pii.csv rows.

    Only for local/analysis use: that file never reaches the cloud, which is the
    whole reason member_status.csv exists. Filters to customerType == MEMBER
    here, since the raw file also holds guests and staff."""
    return select(
        {"climber_key": r.get("customerGuid"),
         "first_name": r.get("firstName"),
         "last_name": r.get("lastName"),
         "email": r.get("email"),
         "status": r.get("currentStatus"),
         "do_not_mail": r.get("doNotMail"),
         "membership_start": r.get("membershipStartDate"),
         "membership_exp": r.get("membershipExpDate")}
        for r in rows
        if str(r.get("customerType") or "").strip().lower() == ACTIVE_TYPE
    )


def load(path) -> tuple[list[Member], Counts]:
    """Read member_status.csv at `path` and select the audience."""
    import csv
    with open(path, encoding="utf-8-sig", newline="") as f:
        return select(list(csv.DictReader(f)))


def survey_link(base_url: str, member_id: str) -> str:
    """The member's personal survey link: the public collector URL plus the
    `mid` custom variable. `mid` is how a response in the Google Sheet gets
    matched back to a member; SurveyMonkey passes it straight through (verified
    2026-08-21, it lands in sheet column S)."""
    if not member_id:
        raise ValueError("survey_link needs a member_id; a blank mid is unmatchable")
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}mid={member_id}"
