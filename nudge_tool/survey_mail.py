"""Render + send one Shift survey email through Brevo (ROADMAP Block 25 A1,
2026-10-23). Called only by survey_dispatch when survey.dispatch.ftv is
"native"; the Mailchimp path never comes here.

The copy is the live Mailchimp journey email, imported by
import_survey_emails.py into clients/<slug>/survey_emails/<tag>.html, keyed by
the SAME tag the engine already picks (FTV_survey_subject_a / _b / _a_embed /
_b_embed, FTV_survey_reminder_embed). So the 2x2 test arms carry over with no
change to who gets which version (decision 5).

Per send:
  embed versions: five 30-day page tokens (native_survey.issue_send_tokens),
                  each button -> /survey-tap?token=... (logs the tap, then the
                  page). Plain versions keep their Google Form link, as live.
  every version:  one opt-out link (survey_optout.issue_links) in the body and
                  footer, plus List-Unsubscribe / List-Unsubscribe-Post headers
                  pointing at the same page, so Gmail's own Unsubscribe button
                  lands on OUR list, not on the Brevo block list ABC shares.
Anything unfilled, any mint failure, any send failure = raise. The caller
reports FAILED and does not log the send; nothing falls back to Mailchimp.
"""
from __future__ import annotations

import html as _html
import json
import re
from pathlib import Path
from urllib.parse import urlencode

from . import native_survey, survey_optout

TAP_PATH = "/survey-tap"
_PLACEHOLDER = re.compile(r"__[A-Z0-9_]+__")


class SurveyMailError(RuntimeError):
    """A survey email could not be built. Messages carry no email address."""


def emails_dir(client) -> Path:
    return Path(client.client_dir) / "survey_emails"


def load_versions(client) -> dict:
    """tag -> {"html", "embed", "subject", "preview", ...} from manifest.json."""
    p = emails_dir(client) / "manifest.json"
    if not p.exists():
        raise SurveyMailError(f"no survey email manifest at {p} "
                              "(run import_survey_emails.py)")
    return dict(json.loads(p.read_text(encoding="utf-8")).get("versions") or {})


def render(client, tag: str, *, first_name: str, email: str,
           tap_urls: dict | None, optout_url: str) -> tuple[str, str]:
    """(subject, html) for one recipient. Raises when the tag has no version,
    an embed version lacks its five URLs, or any placeholder is left over."""
    versions = load_versions(client)
    v = versions.get(tag)
    if not v:
        raise SurveyMailError(f"no Brevo survey email for tag {tag!r}")
    text = (emails_dir(client) / v["html"]).read_text(encoding="utf-8")
    subject = (v.get("subject") or "").strip()
    if not subject:
        raise SurveyMailError(f"{tag}: blank subject")
    esc = lambda s: _html.escape(s or "", quote=True)  # noqa: E731
    fills = {
        "__SUBJECT__": esc(subject),
        "__PREVIEW_TEXT__": esc(v.get("preview") or ""),
        # Mailchimp printed a blank for a missing FNAME ("Hey "); our
        # templates.render() rule is "there", used for every other email.
        "__FIRST_NAME__": esc((first_name or "").strip() or "there"),
        "__EMAIL__": esc(email),
        "__OPTOUT_URL__": esc(optout_url),
    }
    if v.get("embed"):
        if not tap_urls or set(tap_urls) != {1, 2, 3, 4, 5}:
            raise SurveyMailError(f"{tag}: needs five rating-button URLs")
        for n in range(1, 6):
            fills[f"__Q1_URL_{n}__"] = esc(tap_urls[n])
    left = sorted(set(_PLACEHOLDER.findall(text)) - set(fills))
    if left:
        raise SurveyMailError(f"{tag}: unfilled placeholder(s) {', '.join(left)}")
    # one pass over the TEMPLATE's placeholders only, so a value that happens
    # to look like one (a first name "Ann__X__") is never re-read as a slot
    text = _PLACEHOLDER.sub(lambda m: fills.get(m.group(0), m.group(0)), text)
    return subject, text


def send(brevo, client, target, *, db=None, now=None, base_url: str | None = None) -> dict:
    """build() + send in one call (no send ledger: the live path goes through
    survey_dispatch, which records each send). Returns Brevo's response."""
    subject, html, tags, headers = build(client, target, db=db, now=now, base_url=base_url)
    return brevo.send_html(target.email, target.first_name, subject, html,
                           tags=tags, headers=headers)


def build(client, target, *, db=None, now=None, base_url: str | None = None):
    """Mint links and render one survey email for a livesend.SendTarget (or
    anything with email / first_name / climber_id / tag). Returns (subject,
    html, Brevo tags, headers). Writes the tokens to Firestore, sends nothing.
    Raises on any failure."""
    email = (target.email or "").strip()
    if not email:
        raise SurveyMailError("no email")
    base = (native_survey.public_base_url() if base_url is None
            else base_url).rstrip("/")
    if not base:
        raise SurveyMailError("NATIVE_SURVEY_PUBLIC_BASE_URL not set")
    v = load_versions(client).get(target.tag)
    if not v:
        raise SurveyMailError(f"no Brevo survey email for tag {target.tag!r}")
    tap_urls = None
    if v.get("embed"):
        tokens = native_survey.issue_send_tokens(
            client, email, target.climber_id, [1, 2, 3, 4, 5], tag=target.tag,
            db=db, now=now)
        tap_urls = {n: base + TAP_PATH + "?" + urlencode({"token": t})
                    for n, t in tokens.items()}
    page_url, one_click_url = survey_optout.issue_links(
        client, email, db=db, now=now, base_url=base)
    subject, html = render(client, target.tag, first_name=target.first_name,
                           email=email, tap_urls=tap_urls, optout_url=page_url)
    # NOT verified that Brevo keeps these: a Brevo staff answer (community
    # forum) says it adds its own List-Unsubscribe to every transactional
    # email. If it wins, a click lands on Brevo's block list with Shift's
    # sender, which survey_dispatch also treats as an opt-out.
    headers = {"List-Unsubscribe": f"<{one_click_url}>",
               "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
    # gym_<slug>: the shared Brevo account's readers drop another gym's events
    # (brevo_client.member_activity), so ABC never counts a Shift survey
    return subject, html, [target.tag, "send_it_survey", gym_tag(client)], headers


def gym_tag(client) -> str:
    from .brevo_client import GYM_TAG_PREFIX
    return f"{GYM_TAG_PREFIX}{client.slug}"
