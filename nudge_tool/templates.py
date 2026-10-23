"""Email copy lives in clients/<slug>/templates/<template_id>.md as plain text.

File format (easy to edit, no code, no escaping):

    Subject: Ready to come back, {first_name}?

    Hey {first_name},
    ...body...

First line is `Subject: ...`; everything after the blank line is the body.
`{first_name}` is always substituted. Any `{token}` whose name is a key in the
client's `links` block (client.json) is also substituted (e.g. `{survey_link}`,
`{membership_lite_link}`, `{contact_email}`). Unknown `{tokens}` are left as-is.
The tool does NOT
send these; a Mailchimp Customer Journey tied to the trigger tag does. They
live here so the dashboard can preview them and SHIFT can review the exact
words before any Journey is built.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Template:
    template_id: str
    subject: str
    body: str
    subject_b: str = ""  # optional A/B alternate subject (empty = no test)


def parse_template(text: str, template_id: str) -> Template:
    """Parse a template file.

    First `Subject:` line is the primary (A) subject. An optional `Subject B:`
    line (any order) is the A/B alternate; when present the Brevo send path
    randomly picks A or B per recipient. Both `Subject`/`Subject B` lines are
    stripped from the body; everything after the last header line + blank line
    is the body.
    """
    lines = text.splitlines()
    subject = ""
    subject_b = ""
    body_start = 0
    for i, ln in enumerate(lines):
        low = ln.lower()
        if not ln.strip() and (subject or subject_b):
            break  # blank line ends the header block
        if low.startswith("subject b:"):
            subject_b = ln.split(":", 1)[1].strip()
            body_start = max(body_start, i + 1)
        elif low.startswith("subject:"):
            subject = ln.split(":", 1)[1].strip()
            body_start = max(body_start, i + 1)
    while body_start < len(lines) and not lines[body_start].strip():
        body_start += 1
    body = "\n".join(lines[body_start:]).strip()
    return Template(template_id=template_id, subject=subject,
                    body=body, subject_b=subject_b)


def load_templates(templates_dir: Path) -> dict[str, Template]:
    out: dict[str, Template] = {}
    if not templates_dir.exists():
        return out
    for path in sorted(templates_dir.glob("*.md")):
        out[path.stem] = parse_template(path.read_text(encoding="utf-8"), path.stem)
    return out


def render(templates: dict[str, Template], template_id: str,
           first_name: str, links: dict | None = None,
           variant: str = "A") -> tuple[str, str]:
    """Return (subject, body) with {first_name} and any {link_token} substituted.

    `links` maps a token name (without braces) to its replacement value, e.g.
    {"survey_link": "https://..."} substitutes `{survey_link}`. Unknown tokens
    are left untouched.

    `variant` selects the A/B subject: "B" uses `subject_b` when the template
    has one, otherwise it falls back to the primary subject. The body is the
    same for both variants (A/B is a subject-line test only). Default "A" keeps
    callers that don't run an A/B test (dashboard preview, Mailchimp path)
    unchanged.
    """
    t = templates[template_id]
    fn = first_name or "there"
    subs = {"first_name": fn}
    subs.update(links or {})

    def apply(text: str) -> str:
        for key, val in subs.items():
            text = text.replace("{" + key + "}", str(val))
        return text

    subject = t.subject_b if (variant == "B" and t.subject_b) else t.subject
    return (apply(subject), apply(t.body))
