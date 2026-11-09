"""Typo'd and broken email addresses (2026-11-09, Luke).

Why: on 11/08 and 11/09 SHIFT's survey send to `perrymb@gmail.c` was refused by
Brevo ("email is not valid") and retried every day, and ABC emailed
`spiral6824@gmai.com` (11/03, 11/07) and `cmac1704@gmail.cm` (11/08): typo
domains that ACCEPT mail, so a stranger got them and nothing ever bounced.
A scan of both gyms' data on 11/09 found ~25 such domains (gmai.com x9,
gmail.cm x6, gmil.com x6, ...).

Rules (Luke, 11/09):
  - an address whose domain is on the explicit TYPO list below is sent to the
    corrected domain. Brevo emails only. The gym's record (RGP / Beta), our
    outreach log, survey links and every match by email keep the ORIGINAL
    address; only the Brevo "to" changes, and the Brevo stats lookup uses
    the same fix so delivered / opens are counted for the right send.
  - SHIFT's Mailchimp emails (offers, reminders, check-ins) are SKIPPED for
    a typo'd address, never corrected: that would add a new contact to
    Shift's Mailchimp list.
  - an address that is broken and not on the list (no @, no dot in the
    domain, a 1-letter ending) is never sent; the run prints a skip line,
    not FAILED.

The list is explicit on purpose, no fuzzy matching: ymail.com, mac.com,
mail.com, email.com, aim.com, myyahoo.com and chartertn.net are REAL domains
that sit one or two letters from a big provider. Add a typo here only when it
is clearly one (and never a real domain).

Shared module: byte-identical in ABC and SHIFT (project rule 4).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# corrected domain -> misspellings of the NAME part (each also gets every
# ending typo below). Every entry was seen in the gyms' data or is the same
# kind of slip; none is a real mail domain.
_NAME_TYPOS: dict[str, tuple[str, ...]] = {
    "gmail.com": (
        "gmai.com", "gmal.com", "gmil.com", "gamil.com", "gmial.com", "gnail.com",
        "gmali.com", "gmaik.com", "gmaill.com", "gmaail.com", "gmaiil.com", "gmaol.com",
        "gsmil.com", "gmsil.com", "gmaul.com", "gmail-.com", "gmail.com.com",
    ),
    "yahoo.com": ("ahoo.com", "yaho.com", "yahooo.com", "yhoo.com", "yahool.com", "yaoo.com"),
    "hotmail.com": ("hotmial.com", "hotmal.com", "hotmai.com", "hotmil.com", "hotmaill.com",
                    "hotamil.com"),
    "outlook.com": ("outloo.com", "outlok.com", "oulook.com", "outllok.com", "outlool.com"),
    "icloud.com": ("iclould.com", "icoud.com", "icould.com", "ivloud.com", "iclod.com",
                   "icluod.com", "iclud.com", "icloude.com", "icooud.com"),
    "aol.com": (),
    "comcast.net": ("comcast-net", "concast.net", "comcat.net", "comcast.ner", "comcast.nt"),
}
# ".com" slips, applied to each ".com" provider (gmail.cm, yahoo.con, aol.c,
# "gmail" with no ending, ...). None of these is a real domain for them.
_COM_ENDING_TYPOS = (".c", ".cm", ".co", ".con", ".cok", ".om", ".comm", ".come", ".cpm",
                     ".vom", ".xom", ".ocm", ".cim", ".cem", "com", "")


def _build_typos() -> dict[str, str]:
    out: dict[str, str] = {}
    for good, names in _NAME_TYPOS.items():
        for typo in names:
            out[typo] = good
        if good.endswith(".com"):
            base = good[:-4]
            for end in _COM_ENDING_TYPOS:
                out[base + end] = good
    out.pop("", None)
    return out


TYPO_DOMAINS: dict[str, str] = _build_typos()

# what a sendable address must look like after any fix: one @, something
# before it, a domain with at least one dot and a 2+ letter ending
_VALID = re.compile(r"^[^@\s]+@[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\.[a-z]{2,}$")


@dataclass(frozen=True)
class Fix:
    original: str   # exactly as passed in
    address: str    # where a Brevo email should go ("" when invalid)
    status: str     # "ok" | "corrected" | "invalid"
    reason: str = ""


def fix(email) -> Fix:
    """What to do with `email`. "ok" returns it unchanged (as passed: no
    case or space change, so every normal send is byte-for-byte as before)."""
    raw = "" if email is None else str(email)
    s = raw.strip()
    if s.count("@") != 1:
        return Fix(raw, "", "invalid", "not one @ in the address")
    local, domain = s.split("@")
    dom = domain.strip().lower().rstrip(".")
    if not local.strip():
        return Fix(raw, "", "invalid", "nothing before the @")
    if re.search(r"\s", local.strip()):
        return Fix(raw, "", "invalid", "a space inside the address")
    good = TYPO_DOMAINS.get(dom)
    if good:
        return Fix(raw, f"{local.strip()}@{good}", "corrected", f"typo domain {dom} -> {good}")
    if not _VALID.match(f"{local.strip()}@{dom}"):
        return Fix(raw, "", "invalid", f"domain '{domain.strip()}' is not a real address ending")
    if domain.strip() != domain.strip().rstrip("."):
        return Fix(raw, f"{local.strip()}@{dom}", "corrected", "trailing dot removed")
    return Fix(raw, raw, "ok")


def send_address(email) -> str:
    """The address a Brevo email to `email` really went to: the corrected one
    for a typo domain, otherwise `email` unchanged (invalid included, so a
    lookup just finds nothing)."""
    f = fix(email)
    return f.address if f.status == "corrected" else ("" if email is None else str(email))


def is_typo(email) -> bool:
    """True for a corrected OR invalid address (SHIFT skips its Mailchimp
    email for both)."""
    return fix(email).status != "ok"
