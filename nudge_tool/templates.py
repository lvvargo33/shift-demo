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


def parse_template(text: str, template_id: str) -> Template:
    lines = text.splitlines()
    subject = ""
    body_start = 0
    for i, ln in enumerate(lines):
        if ln.lower().startswith("subject:"):
            subject = ln.split(":", 1)[1].strip()
            body_start = i + 1
            break
    while body_start < len(lines) and not lines[body_start].strip():
        body_start += 1
    body = "\n".join(lines[body_start:]).strip()
    return Template(template_id=template_id, subject=subject, body=body)


def load_templates(templates_dir: Path) -> dict[str, Template]:
    out: dict[str, Template] = {}
    if not templates_dir.exists():
        return out
    for path in sorted(templates_dir.glob("*.md")):
        out[path.stem] = parse_template(path.read_text(encoding="utf-8"), path.stem)
    return out


def render(templates: dict[str, Template], template_id: str,
           first_name: str, links: dict | None = None) -> tuple[str, str]:
    """Return (subject, body) with {first_name} and any {link_token} substituted.

    `links` maps a token name (without braces) to its replacement value, e.g.
    {"survey_link": "https://..."} substitutes `{survey_link}`. Unknown tokens
    are left untouched.
    """
    t = templates[template_id]
    fn = first_name or "there"
    subs = {"first_name": fn}
    subs.update(links or {})

    def apply(text: str) -> str:
        for key, val in subs.items():
            text = text.replace("{" + key + "}", str(val))
        return text

    return (apply(t.subject), apply(t.body))
