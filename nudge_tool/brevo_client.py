"""Thin Brevo (formerly Sendinblue) transactional client.

Appalachian sends through Brevo instead of Mailchimp (SPEC Addition 2026-06-22):
Mailchimp's tag-triggered Customer Journeys now require the paid Standard plan,
while Brevo's free plan (2,000 contacts, 300 sends/day) covers ABC's ~15-28/day
at $0/mo. The copy lives in the tool (the 12 forked templates), so there is no
ESP-side Journey to hold it: the tool renders each email and sends it DIRECTLY
via Brevo's transactional API. There are no tags or automations in Brevo.

This class mirrors MailchimpClient's PUBLIC shape so the call sites in cli.py and
send_nudges.py are ESP-agnostic (selected by esp.make_client):
  ping() / get_audience()        -> GET /account, GET /contacts (selftest reads)
  suppressed_emails()            -> opt-outs to never send to (marketing blocklist
                                    + transactional blocked/unsubscribed set)
  tag_member(email, fn, tag)     -> upsert the contact, then SEND the rendered
                                    template that `tag` maps to. The Mailchimp
                                    notion of "apply a tag so a Journey sends" is
                                    collapsed into "render + send" here.
  archive_member(email)          -> delete the contact (test-dummy cleanup)
  remove_tag(email, tag)         -> no-op (Brevo has no tags; cleanup just deletes)

Built on `requests` (no SDK), same as mailchimp_client. The double-lock gating
lives at the call sites, unchanged; this class only does what it is told.

API shape used (base https://api.brevo.com/v3):
  GET    /account                         account + plan (auth/selftest)
  GET    /contacts?limit&offset           list contacts (marketing blocklist read)
  GET    /smtp/blockedContacts            transactional blocks / unsubscribes
  POST   /contacts {email,attributes}     upsert (updateEnabled:true)
  POST   /smtp/email {sender,to,...}      send one transactional email
  DELETE /contacts/{email}                delete a contact
"""
from __future__ import annotations

import hashlib
import html as _html
import re
import time
from dataclasses import dataclass
from urllib.parse import quote

import requests

from .config import ESPError, Settings
from .templates import render as render_template

BASE = "https://api.brevo.com/v3"
DEFAULT_TIMEOUT = 30
# Brevo's transactional event log: "past 30 days by default", `days` max 90.
EVENT_WINDOW_DAYS = 90
# A 429 on a READ is retried once if Brevo says the wait is this short or less.
# Brevo's limits are per HOUR (docs: "all other endpoints" 100 requests/hour on
# the free tier; the events endpoint let ~300 through on 2026-09-11 before the
# 429), so a long Retry-After means "not this run": the caller keeps what it
# fetched and asks for the rest tomorrow. Never applied to sends.
RATE_LIMIT_MAX_WAIT = 60.0

# Branded HTML shell (Appalachian "dark-bold" template). Each email's rendered
# copy drops into __BODY__, its subject becomes the __HEADLINE__, the primary
# link (if any) becomes the orange CTA at __CTA__, and the footer carries the
# real contact email + Brevo's working {{ unsubscribe }} tag. Placeholders are
# filled by str.replace (NOT .format/f-string) so the CSS braces and the
# {{ unsubscribe }} double-braces survive intact. Mailchimp-only tags from the
# original export (*|ARCHIVE|*, *|UPDATE_PROFILE|*) and the Cloudflare email
# obfuscation were removed; only Brevo-valid tokens remain.
_SHELL = '''<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="X-UA-Compatible" content="IE=edge">
  <meta name="format-detection" content="telephone=no, date=no, address=no, email=no">
  <title>Appalachian Bouldering Center</title>
  <!--[if mso]>
  <noscript><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml></noscript>
  <![endif]-->
  <style>
    body { margin:0; padding:0; width:100% !important; -webkit-text-size-adjust:100%; -ms-text-size-adjust:100%; }
    table { border-collapse:collapse; mso-table-lspace:0; mso-table-rspace:0; }
    img { border:0; outline:none; text-decoration:none; -ms-interpolation-mode:bicubic; display:block; }
    a { text-decoration:none; }
    .btn:hover { background:#f06a3e !important; }
    .lnk:hover { color:#ffffff !important; }
    @media only screen and (max-width:620px) {
      .container { width:100% !important; }
      .px { padding-left:28px !important; padding-right:28px !important; }
      .h1 { font-size:30px !important; line-height:36px !important; }
    }
  </style>
</head>
<body style="margin:0; padding:0; background-color:#0b0b0b;">
  <div style="display:none; max-height:0; overflow:hidden; mso-hide:all; font-size:1px; line-height:1px; color:#0b0b0b; opacity:0;">
    A quick note from Appalachian Bouldering Center &mdash; see you on the wall.
    &nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;
  </div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#0b0b0b;">
    <tr>
      <td align="center" style="padding:40px 16px;">
        <table role="presentation" class="container" width="600" cellpadding="0" cellspacing="0" style="width:600px; max-width:600px; background-color:#171717;">
          <tr>
            <td align="center" class="px" style="padding:56px 48px 24px 48px;">
              <img src="https://images.squarespace-cdn.com/content/v1/6446fa39ef791c25ce8ba7ec/0c5e91e8-e9ea-4379-8d74-b917535f2d71/ABC_LogoWhite.png?format=750w" width="248" alt="Appalachian Bouldering Center" style="width:248px; max-width:68%; height:auto; margin:0 auto;">
            </td>
          </tr>
          <tr>
            <td align="center" class="px" style="padding:0 48px 28px 48px; font-family:Arial,Helvetica,sans-serif; font-size:11px; letter-spacing:3px; text-transform:uppercase; color:#b6b1a8;">
              Est. 2023 &nbsp;&middot;&nbsp; Bristol, TN/VA
            </td>
          </tr>
          <tr>
            <td align="center" style="padding:0 48px;">
              <table role="presentation" cellpadding="0" cellspacing="0"><tr>
                <td style="width:40px; height:2px; line-height:2px; font-size:2px; background-color:#e2552b;">&nbsp;</td>
              </tr></table>
            </td>
          </tr>
          <tr>
            <td class="px" style="padding:38px 48px 0 48px;">
              <p style="margin:0 0 14px 0; font-family:Arial,Helvetica,sans-serif; font-size:11px; font-weight:bold; letter-spacing:4px; text-transform:uppercase; color:#e2552b;">A Note From The Team</p>
              <h1 class="h1" style="margin:0; font-family:'Arial Black',Arial,Helvetica,sans-serif; font-size:34px; line-height:40px; font-weight:900; letter-spacing:-0.4px; text-transform:uppercase; color:#f5f3f0;">__HEADLINE__</h1>
            </td>
          </tr>
          <tr>
            <td class="px" style="padding:24px 48px 0 48px;">
              <table role="presentation" cellpadding="0" cellspacing="0"><tr>
                <td style="width:44px; height:2px; line-height:2px; font-size:2px; background-color:#e2552b;">&nbsp;</td>
              </tr></table>
            </td>
          </tr>
          <tr>
            <td class="px" style="padding:24px 48px 0 48px; font-family:Georgia,'Times New Roman',serif; font-size:16px; line-height:28px; color:#bdb9b2;">
              __BODY__
            </td>
          </tr>
          <tr><td style="padding:30px 0 0 0;">&nbsp;</td></tr>
        </table>
        <table role="presentation" class="container" width="600" cellpadding="0" cellspacing="0" style="width:600px; max-width:600px;">
          <tr>
            <td align="center" class="px" style="padding:34px 48px 22px 48px;">
              <a href="https://www.instagram.com/appalachianboulderingcenter/?hl=en" class="lnk" style="font-family:Arial,Helvetica,sans-serif; font-size:11px; font-weight:bold; letter-spacing:2px; text-transform:uppercase; color:#9a9a9a; padding:0 12px;">Instagram</a>
              <span style="color:#e2552b;">&middot;</span>
              <a href="https://www.facebook.com/p/Appalachian-Bouldering-Center-100090150902789/" class="lnk" style="font-family:Arial,Helvetica,sans-serif; font-size:11px; font-weight:bold; letter-spacing:2px; text-transform:uppercase; color:#9a9a9a; padding:0 12px;">Facebook</a>
              <span style="color:#e2552b;">&middot;</span>
              <a href="https://www.appalachianboulderingcenter.com/" class="lnk" style="font-family:Arial,Helvetica,sans-serif; font-size:11px; font-weight:bold; letter-spacing:2px; text-transform:uppercase; color:#9a9a9a; padding:0 12px;">Website</a>
            </td>
          </tr>
          <tr>
            <td align="center" style="padding:0 48px;">
              <table role="presentation" width="120" cellpadding="0" cellspacing="0" style="width:120px;"><tr>
                <td style="height:1px; line-height:1px; font-size:1px; background-color:#262626;">&nbsp;</td>
              </tr></table>
            </td>
          </tr>
          <tr>
            <td align="center" class="px" style="padding:22px 48px 0 48px; font-family:Arial,Helvetica,sans-serif; font-size:12px; line-height:21px; color:#6f6f6f;">
              <strong style="color:#8f8f8f; letter-spacing:0.5px;">Appalachian Bouldering Center</strong><br>
              1225 Volunteer Parkway, Bristol, TN 37620<br>
              (423) 797-4477 &nbsp;&middot;&nbsp; <a href="mailto:__CONTACT__" class="lnk" style="color:#6f6f6f; text-decoration:underline;">__CONTACT__</a>
            </td>
          </tr>
          <tr>
            <td align="center" class="px" style="padding:20px 48px 0 48px; font-family:Arial,Helvetica,sans-serif; font-size:11px; line-height:18px; color:#565656;">
              You're receiving this email because you visited Appalachian Bouldering Center.<br>
              <a href="__UNSUB__" class="lnk" style="color:#565656; text-decoration:underline;">Unsubscribe</a>
            </td>
          </tr>
          <tr><td style="padding:18px 0 0 0;">&nbsp;</td></tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>'''


@dataclass
class BrevoError(ESPError):
    status: int
    detail: str

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"Brevo API {self.status}: {self.detail}"


# A send that carries "gym_<client slug>" among its Brevo tags belongs to that
# gym. SHIFT's survey sends carry "gym_shift"; ABC's sends carry none (yet).
# member_activity drops events tagged for a different gym (shared account).
GYM_TAG_PREFIX = "gym_"


def _tag_tokens(tag) -> set[str]:
    """Brevo hands a multi-tag send's tags back joined ("a|b")."""
    return {t.strip() for t in re.split(r"[|,]", str(tag or "")) if t.strip()}


@dataclass
class BrevoRateLimited(BrevoError):
    """HTTP 429. `retry_after` = seconds Brevo asked us to wait (None when the
    header is missing or not a number); `limits` = every rate-limit header on
    the response, verbatim, so the log shows which bucket we hit."""
    retry_after: float | None = None
    limits: str = ""

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        extra = f"; retry-after={self.retry_after}s" if self.retry_after is not None else ""
        if self.limits:
            extra += f"; {self.limits}"
        return f"Brevo API 429: {self.detail or 'rate limited'}{extra}"


def _rate_limit_info(headers) -> tuple[float | None, str]:
    """(retry_after_seconds, 'name=value ...' of the rate-limit headers)."""
    retry: float | None = None
    try:
        raw = headers.get("Retry-After")
        if raw is not None and str(raw).strip().isdigit():
            retry = float(raw)
    except Exception:  # noqa: BLE001 - diagnostics only
        retry = None
    parts = []
    try:
        for k, v in headers.items():
            kl = k.lower()
            if "ratelimit" in kl or kl == "retry-after":
                parts.append(f"{k}={v}")
    except Exception:  # noqa: BLE001
        pass
    return retry, " ".join(sorted(parts))


class BrevoClient:
    def __init__(self, settings: Settings, client, timeout: int = DEFAULT_TIMEOUT):
        """`client` is the ClientConfig: the Brevo path needs its templates +
        link tokens (to render copy) and its triggers (to map a tag back to the
        template to send), things the Mailchimp path left to an ESP-side Journey."""
        self.s = settings
        self.client = client
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "api-key": settings.api_key,
            "accept": "application/json",
            "content-type": "application/json",
        })
        # tag -> template_id, derived from the client's triggers. The send target
        # carries the tag (same as Mailchimp); here it selects which copy to send.
        self._tag_to_template = {t.tag: t.template_id for t in client.triggers}
        self._account_email: str | None = None  # lazily resolved send-from fallback

    # ---- low-level ------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs) -> dict:
        resp = self._session.request(
            method, f"{BASE}{path}", timeout=self.timeout, **kwargs
        )
        if resp.status_code >= 400:
            detail = resp.text
            try:
                body = resp.json()
                # Brevo errors look like {"code": "...", "message": "..."}
                detail = body.get("message", detail)
            except ValueError:
                pass
            if resp.status_code == 429:
                retry, limits = _rate_limit_info(resp.headers)
                raise BrevoRateLimited(429, detail, retry_after=retry, limits=limits)
            raise BrevoError(resp.status_code, detail)
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    # ---- read (always safe) --------------------------------------------
    def ping(self) -> dict:
        """GET /account — account + plan. Cheapest auth check (backs selftest)."""
        return self._request("GET", "/account")

    def get_audience(self) -> dict:
        """Contacts overview. Brevo has no Mailchimp-style 'audience'; we report
        the contact count so selftest has something to print."""
        body = self._request("GET", "/contacts", params={"limit": 1})
        return {"name": "Brevo contacts", "count": body.get("count", 0)}

    def _marketing_blocklist(self) -> set[str]:
        out: set[str] = set()
        offset, limit = 0, 500
        while True:
            body = self._request("GET", "/contacts",
                                  params={"limit": limit, "offset": offset})
            contacts = body.get("contacts", [])
            for c in contacts:
                if c.get("emailBlacklisted"):
                    e = (c.get("email") or "").strip().lower()
                    if e:
                        out.add(e)
            offset += len(contacts)
            if not contacts or offset >= body.get("count", 0):
                break
        return out

    def _transactional_blocklist(self) -> set[str]:
        """Addresses Brevo blocks for transactional mail (hard bounces, and
        anyone who used the {{ unsubscribe }} link on a prior send). These must
        be skipped on the next run for compliance."""
        out: set[str] = set()
        offset, limit = 0, 100  # /smtp/blockedContacts caps limit at 100 (else 400)
        while True:
            body = self._request("GET", "/smtp/blockedContacts",
                                  params={"limit": limit, "offset": offset})
            contacts = body.get("contacts", [])
            for c in contacts:
                e = (c.get("email") or "").strip().lower()
                if e:
                    out.add(e)
            offset += len(contacts)
            if not contacts or offset >= body.get("count", 0):
                break
        return out

    def member_activity(self, email: str) -> list[dict]:
        """Delivery/open/click events for one contact, normalized to the shape
        engine._has_event expects: {"activity_type":
        "delivered"|"open"|"click", "created_at_timestamp": <ISO date>,
        "link_clicked": <url>} (the same shape Mailchimp's activity feed
        returns, so the engine is ESP-agnostic).

        GET /smtp/statistics/events filtered by email is Brevo's transactional
        event log (newest first, capped at 100 here). Without a date range
        Brevo returns only the PAST 30 DAYS (documented default); `days` is
        the widest single window it allows (90, "maximum 90"), so ask for
        that explicitly. The stats push freezes a send's result inside its
        14-day cache window, so 90 days is plenty; this just stops the count
        from silently depending on the default. A 404 (no events for this
        address) returns [] instead of raising.

        A 429 is retried ONCE when Brevo's Retry-After is short (<=
        RATE_LIMIT_MAX_WAIT); otherwise, or on a second 429, the
        BrevoRateLimited propagates so the caller can stop the loop, keep
        what it has, and log the limit headers (2026-09-11: the stats push
        hit a 429 at feed 300 of 317 and threw away all 300 answers)."""
        params = {"email": email, "limit": 100, "days": EVENT_WINDOW_DAYS,
                  "sort": "desc"}
        body: dict = {}
        for attempt in (1, 2):
            try:
                body = self._request("GET", "/smtp/statistics/events", params=params)
                break
            except BrevoRateLimited as e:
                wait = e.retry_after
                if attempt == 1 and wait is not None and 0 < wait <= RATE_LIMIT_MAX_WAIT:
                    time.sleep(wait)
                    continue
                raise
            except BrevoError as e:
                if e.status == 404:
                    return []
                raise
        out: list[dict] = []
        own_gym = f"{GYM_TAG_PREFIX}{getattr(getattr(self, 'client', None), 'slug', '')}"
        for ev in body.get("events", []) or []:
            # ABC and SHIFT share this Brevo account and some subject lines
            # ("Thanks for climbing with us!"), and an event's subject alone
            # can pin it to a template. A send tagged for ANOTHER gym is that
            # gym's email: drop it here so no caller can count it (Block 25
            # A1 tester round, 2026-10-23). Untagged-gym events (every ABC
            # send today) are kept as before.
            gyms = {t for t in _tag_tokens(ev.get("tag", "")) if t.startswith(GYM_TAG_PREFIX)}
            if gyms and own_gym not in gyms:
                continue
            kind = ev.get("event")
            # tag + subject identify WHICH email the event belongs to: sends
            # tag every message [template_id, template_id_<A|B>], and the
            # subject is the rendered template subject. engine._has_event
            # uses them to pin an open to the specific email (SHIFT S31
            # parity); events missing both degrade to date-only counting.
            ident = {"tag": ev.get("tag", "") or "",
                     "subject": ev.get("subject", "") or ""}
            if kind == "opened":
                out.append({"activity_type": "open",
                            "created_at_timestamp": ev.get("date", ""),
                            **ident})
            elif kind == "loadedByProxy":
                # An open that came through a mail proxy (Apple Mail privacy
                # protection loads every email for its users). Brevo files it
                # under its own event name; Mailchimp counts the same thing as
                # a normal open at SHIFT. Dropping it (until 2026-09-17) read
                # every Apple address as never opened: 0 of 63 at ABC against
                # 52% at SHIFT. Counted as an open so both gyms share one
                # definition; "proxy" keeps the kind so the stats cache can
                # still tell a confirmed open from an automatic one.
                out.append({"activity_type": "open", "proxy": True,
                            "created_at_timestamp": ev.get("date", ""),
                            **ident})
            elif kind == "delivered":
                out.append({"activity_type": "delivered",
                            "created_at_timestamp": ev.get("date", ""),
                            **ident})
            elif kind == "clicks":
                out.append({"activity_type": "click",
                            "created_at_timestamp": ev.get("date", ""),
                            "link_clicked": ev.get("link", ""),
                            **ident})
            elif kind == "unsubscribed":
                # The person used the {{ unsubscribe }} link in THIS email
                # (tag + subject say which one). Tasks step 1.3 (2026-09-18):
                # the stats push pins it to that send, the same way an open
                # is pinned. /smtp/blockedContacts also lists the address but
                # cannot say which email was unsubscribed from, so it stays
                # the send-time suppression source only.
                out.append({"activity_type": "unsubscribe",
                            "created_at_timestamp": ev.get("date", ""),
                            **ident})
        return out

    def suppressed_emails(self) -> set[str]:
        """Everyone we must NOT send to: marketing opt-outs (emailBlacklisted)
        UNION the transactional block/unsubscribe set. Same contract as
        MailchimpClient.suppressed_emails (precedence rule 5)."""
        return self._marketing_blocklist() | self._transactional_blocklist()

    def blocked_contacts(self) -> list[dict]:
        """Every transactional block WITH its reason, for a caller that must
        tell a hard bounce from an unsubscribe: [{"email", "reason",
        "sender"}], reason = Brevo's code (hardBounce, contactFlaggedAsSpam,
        adminBlocked, unsubscribedViaEmail, ...), "" when Brevo gives none.

        Why (ROADMAP Block 25 A1, 2026-10-23): ABC and SHIFT share ONE Brevo
        account, so an ABC unsubscribe sits on the same list as a SHIFT hard
        bounce. SHIFT's survey path keeps only the delivery blocks and runs
        its own opt-out list. ABC keeps using suppressed_emails(), unchanged."""
        out: list[dict] = []
        offset, limit = 0, 100  # same 100 cap as _transactional_blocklist
        while True:
            body = self._request("GET", "/smtp/blockedContacts",
                                  params={"limit": limit, "offset": offset})
            contacts = body.get("contacts", [])
            for c in contacts:
                e = (c.get("email") or "").strip().lower()
                if not e:
                    continue
                reason = c.get("reason") or {}
                code = reason.get("code", "") if isinstance(reason, dict) else str(reason)
                out.append({"email": e, "reason": code or "",
                            "sender": (c.get("senderEmail") or "").strip().lower()})
            offset += len(contacts)
            if not contacts or offset >= body.get("count", 0):
                break
        return out

    # ---- send (caller must gate behind the double-lock) ----------------
    def _sender(self) -> dict:
        """From identity for a send. Prefer the configured BREVO_SEND_FROM; fall
        back to the Brevo account's own login email (resolved once)."""
        email = self.s.sender_email
        if not email:
            if self._account_email is None:
                self._account_email = (self.ping().get("email") or "")
            email = self._account_email
        return {"name": self.s.sender_name or self.client.client_name, "email": email}

    def _reply_to(self) -> dict:
        """Where a recipient's Reply goes, or {} to leave the field off the
        payload entirely.

        Brevo defaults Reply-To to the sender when `replyTo` is absent, so an
        empty setting must OMIT the key rather than send a blank one (a blank
        would be rejected). config.load_settings already defaults
        `reply_to_email` to the send-from address, so this is only non-trivial
        when BREVO_REPLY_TO deliberately splits the two.

        Deliberately does NOT fall back to the Brevo account login email the way
        `_sender()` does: that fallback address is lvvargo33@gmail.com, and a
        member replying to ABC must never land in Luke's personal inbox. No
        configured value = no field = Brevo's own sender default."""
        email = (self.s.reply_to_email or "").strip()
        if not email:
            return {}
        return {"name": self.s.sender_name or self.client.client_name, "email": email}

    _LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

    def _html_body(self, subject: str, body: str) -> str:
        """Wrap the rendered template copy in the branded `_SHELL`. The subject
        becomes the headline. Markdown links render as inline bold-orange
        anchors (nice link text, never a raw URL). The offer ask is bolded +
        orange wherever it appears in the copy so it pops: the "show this
        email at the front desk" phrase (ABC redeems by showing the email,
        2026-07-09) plus the legacy `daypass_code` link value if any copy
        still carries it. `{{ unsubscribe }}` is Brevo's merge tag (proven
        inboxing in _brevo_probe); it is injected by str.replace, NOT an
        f-string, so the double braces survive."""
        code = (self.client.links.get("daypass_code") or "").strip()
        paras = []
        for block in body.split("\n\n"):
            block = block.strip()
            if not block:
                continue
            esc = _html.escape(block).replace("\n", "<br>")
            # markdown links -> inline bold-orange anchors (escape-first keeps
            # text/url safe; an `&` in a query string is now `&amp;`).
            esc = self._LINK_RE.sub(
                r'<a href="\2" style="color:#e2552b; font-weight:bold; '
                r'text-decoration:underline;">\1</a>', esc)
            # the offer ask -> bold + orange so it stands out in the sentence.
            patterns = [r"\bshow this email at the front desk\b"]
            if code:
                patterns.append(r"\b" + re.escape(code) + r"\b")
            for pat in patterns:
                esc = re.sub(
                    pat,
                    lambda m: '<strong style="color:#e2552b;">'
                              + m.group(0) + '</strong>',
                    esc, flags=re.IGNORECASE)
            paras.append('<p style="margin:0 0 20px 0;">' + esc + '</p>')
        body_html = "\n              ".join(paras)

        contact = self.client.links.get("contact_email", "") or self._account_email
        return (_SHELL
                .replace("__HEADLINE__", _html.escape(subject))
                .replace("__BODY__", body_html)
                .replace("__CONTACT__", _html.escape(contact))
                .replace("__UNSUB__", "{{ unsubscribe }}"))

    def upsert_member(self, email: str, first_name: str = "") -> dict:
        """Create or update a contact (POST /contacts with updateEnabled)."""
        attrs = {}
        if first_name:
            attrs["FNAME"] = first_name
        return self._request("POST", "/contacts", json={
            "email": email,
            "attributes": attrs,
            "updateEnabled": True,
        })

    @staticmethod
    def _ab_variant(email: str, template_id: str) -> str:
        """Deterministic A/B bucket for a recipient+template, "A" or "B".

        Hashing (email + template_id) gives a stable ~50/50 split with NO stored
        state: the same climber always lands in the same bucket for a given
        email (so a resend never flips their subject and muddies the test), yet
        a person can be A on one email type and B on another (independent per
        template). sha256 (not Python's per-process-salted hash) keeps the
        bucket reproducible across runs and machines, so tests are deterministic."""
        h = hashlib.sha256(f"{email.strip().lower()}:{template_id}".encode()).hexdigest()
        return "A" if int(h, 16) % 2 == 0 else "B"

    def send_email(self, email: str, first_name: str, template_id: str,
                   extra_links: dict | None = None) -> dict:
        """Render the tool's template for `template_id` and send it via the
        transactional API. When the template carries a `Subject B:` alternate,
        the recipient is bucketed A/B (deterministic, see `_ab_variant`) and the
        send is tagged `{template_id}_{variant}` so Brevo's transactional stats
        compare open rates per subject line. Raises BrevoError if the template is
        missing or the rendered copy still has a leftover {token} (fail closed
        rather than mail a half-rendered email)."""
        if template_id not in self.client.templates:
            raise BrevoError(0, f"no template '{template_id}' for this client")
        has_ab = bool(self.client.templates[template_id].subject_b)
        variant = self._ab_variant(email, template_id) if has_ab else "A"
        # `extra_links` carries PER-RECIPIENT tokens, which client.links cannot
        # hold because its values are the same for everyone. The membership
        # survey needs one: {member_survey_link} embeds the member's own id, and
        # a response with the wrong id (or none) is unmatchable in the sheet.
        # Deliberately merged OVER client.links so a per-send value always wins.
        links = dict(self.client.links)
        if extra_links:
            links.update(extra_links)
        subject, body = render_template(
            self.client.templates, template_id, first_name,
            links, variant=variant)
        rendered = subject + "\n" + body
        if "{" in rendered and "}" in rendered:
            # leftover {first_name}/{link_token} would ship a broken email
            leftovers = [seg.split("}")[0] for seg in rendered.split("{")[1:]
                         if "}" in seg]
            if leftovers:
                raise BrevoError(0, f"unrendered token(s) in {template_id}: "
                                    f"{', '.join('{' + l + '}' for l in leftovers)}")
        # Brevo transactional tags: always tag the template (per-email totals);
        # add the A/B bucket tag only when a test is actually running on it.
        tags = [template_id]
        if has_ab:
            tags.append(f"{template_id}_{variant}")
        payload = {
            "sender": self._sender(),
            "to": [{"email": email, "name": first_name or email}],
            "subject": subject,
            "htmlContent": self._html_body(subject, body),
            "tags": tags,
        }
        reply_to = self._reply_to()
        if reply_to:
            payload["replyTo"] = reply_to
        return self._request("POST", "/smtp/email", json=payload)

    def send_custom(self, email: str, first_name: str, template_id: str,
                    subject: str, body: str) -> dict:
        """Send a MEMBER-FACING email whose copy was edited by a staffer on
        the response page (roadmap block 3, 2026-09). Same From/Reply-To,
        same branded shell and markdown-link handling as `send_email`, but
        the subject and body come from the caller instead of the template
        file, so no token rendering and no A/B bucketing. Tagged with the
        template id it started from, so Brevo's per-template stats still
        count it, plus `edited` so an edited send can be told apart. Refuses
        a blank subject or body before any network call."""
        if not (subject or "").strip():
            raise BrevoError(0, "send_custom: no subject")
        if not (body or "").strip():
            raise BrevoError(0, "send_custom: no body")
        payload = {
            "sender": self._sender(),
            "to": [{"email": email, "name": first_name or email}],
            "subject": subject.strip(),
            "htmlContent": self._html_body(subject.strip(), body),
            "tags": [template_id, "edited"] if template_id else ["edited"],
        }
        reply_to = self._reply_to()
        if reply_to:
            payload["replyTo"] = reply_to
        return self._request("POST", "/smtp/email", json=payload)

    def send_internal(self, to: str, subject: str, body: str,
                      tag: str = "internal") -> dict:
        """Send a STAFF-FACING email (the front-desk check-in alert,
        2026-08-21) to `to`: same From/Reply-To identity and branded shell as
        a member email, but no template file and no token rendering, because
        the copy is built in code from data. `body` is plain paragraphs
        separated by blank lines; markdown links become anchors. Refuses a
        blank recipient or subject before any network call."""
        to = (to or "").strip().lower()
        if not to:
            raise BrevoError(0, "send_internal: no recipient")
        if not (subject or "").strip():
            raise BrevoError(0, "send_internal: no subject")
        payload = {
            "sender": self._sender(),
            "to": [{"email": to, "name": to}],
            "subject": subject,
            "htmlContent": self._html_body(subject, body or ""),
            "tags": [tag] if tag else [],
        }
        reply_to = self._reply_to()
        if reply_to:
            payload["replyTo"] = reply_to
        return self._request("POST", "/smtp/email", json=payload)

    def send_html(self, email: str, first_name: str, subject: str, html: str,
                  tags: list[str] | None = None,
                  headers: dict | None = None) -> dict:
        """Send caller-built HTML as is: same From / Reply-To as every other
        send, but no template file, no `_SHELL`, no token rendering. For copy
        that must match an email built elsewhere byte for byte (SHIFT's survey
        emails, copied from the live Mailchimp journeys; ROADMAP Block 25 A1,
        2026-10-23). `headers` go on the message as given (SHIFT sets its own
        List-Unsubscribe). Refuses a blank recipient, subject or body before
        any network call. ABC does not call this."""
        email = (email or "").strip()
        if not email:
            raise BrevoError(0, "send_html: no recipient")
        if not (subject or "").strip():
            raise BrevoError(0, "send_html: no subject")
        if not (html or "").strip():
            raise BrevoError(0, "send_html: no body")
        payload = {
            "sender": self._sender(),
            "to": [{"email": email, "name": first_name or email}],
            "subject": subject.strip(),
            "htmlContent": html,
            "tags": [t for t in (tags or []) if t],
        }
        if headers:
            payload["headers"] = dict(headers)
        reply_to = self._reply_to()
        if reply_to:
            payload["replyTo"] = reply_to
        return self._request("POST", "/smtp/email", json=payload)

    def tag_member(self, email: str, first_name: str, tag: str) -> dict:
        """The 'mark this climber for nudge X' op. On Brevo that means: upsert the
        contact, then SEND the email the tag maps to. Same signature as
        MailchimpClient.tag_member so the call sites are unchanged."""
        template_id = self._tag_to_template.get(tag)
        if not template_id:
            raise BrevoError(0, f"no trigger maps tag '{tag}' to a template")
        self.upsert_member(email, first_name)
        return self.send_email(email, first_name, template_id)

    def remove_tag(self, email: str, tag: str) -> dict:
        """No-op: Brevo has no tags. Present so cli.py cleanup (which untags then
        archives Mailchimp dummies) works unchanged; cleanup's archive_member
        does the real removal."""
        return {}

    def archive_member(self, email: str) -> dict:
        """DELETE a contact — used by cleanup to clear test dummies."""
        return self._request("DELETE", f"/contacts/{quote(email, safe='')}")
