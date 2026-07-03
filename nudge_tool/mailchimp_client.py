"""Thin Mailchimp Marketing API v3 client: ping, upsert member, apply tag.

Built on `requests` (no SDK) to keep the dependency surface tiny. Write methods
(upsert/tag) are guarded by an explicit `live` flag at the call site in cli.py;
this class itself only does what it's told. Read methods (ping, get_audience)
are always safe and back the connection self-test.

API shape used:
  GET  /                                   -> account / ping
  GET  /lists/{audience_id}                -> audience stats
  GET  /lists/{id}/members/{subhash}/activity-feed -> recent sends/opens/clicks
  PUT  /lists/{id}/members/{subhash}       -> upsert (create or update) a member
  POST /lists/{id}/members/{subhash}/tags  -> add/remove tags on a member
where subhash = md5(lowercased email).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

import requests

from .config import Settings

DEFAULT_TIMEOUT = 30


def subscriber_hash(email: str) -> str:
    """Mailchimp subscriber hash = MD5 of the lowercased, trimmed email."""
    return hashlib.md5(email.strip().lower().encode("utf-8")).hexdigest()


@dataclass
class MailchimpError(Exception):
    status: int
    detail: str

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"Mailchimp API {self.status}: {self.detail}"


class MailchimpClient:
    def __init__(self, settings: Settings, timeout: int = DEFAULT_TIMEOUT):
        self.s = settings
        self.timeout = timeout
        self._session = requests.Session()
        # Mailchimp uses HTTP basic auth; username is arbitrary, password is the key.
        self._session.auth = ("anystring", self.s.api_key)
        self._session.headers.update({"Content-Type": "application/json"})

    # ---- low-level ------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self.s.api_base}{path}"

    def _request(self, method: str, path: str, **kwargs) -> dict:
        resp = self._session.request(
            method, self._url(path), timeout=self.timeout, **kwargs
        )
        if resp.status_code >= 400:
            detail = resp.text
            try:
                body = resp.json()
                detail = body.get("detail", detail)
            except ValueError:
                pass
            raise MailchimpError(resp.status_code, detail)
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    # ---- read (always safe) --------------------------------------------
    def ping(self) -> dict:
        """GET / — returns account metadata. Cheapest auth check."""
        return self._request("GET", "/")

    def get_audience(self) -> dict:
        return self._request("GET", f"/lists/{self.s.audience_id}")

    def get_member(self, email: str) -> dict:
        return self._request(
            "GET", f"/lists/{self.s.audience_id}/members/{subscriber_hash(email)}"
        )

    def member_activity(self, email: str) -> list[dict]:
        """Recent activity events for one contact (sends/opens/clicks, newest
        first; journey emails included). Click events carry the URL clicked in
        `link_clicked`, which is how the Insights engagement slides tell a
        buy-link click from a survey-link click. A contact Mailchimp doesn't
        know (404) -> [] rather than an error. Read-only."""
        try:
            body = self._request(
                "GET",
                f"/lists/{self.s.audience_id}/members/{subscriber_hash(email)}/activity-feed",
                params={"count": 50},
            )
        except MailchimpError as e:
            if e.status == 404:
                return []
            raise
        return body.get("activity", []) or []

    def list_emails_by_status(self, status: str, page: int = 1000) -> set[str]:
        """Lowercased emails of every member in one status (unsubscribed /
        cleaned / archived ...). Paginated; pulls only the email + status fields
        to keep the payload tiny. Read-only."""
        emails: set[str] = set()
        offset = 0
        while True:
            body = self._request(
                "GET", f"/lists/{self.s.audience_id}/members",
                params={
                    "status": status,
                    "count": page,
                    "offset": offset,
                    "fields": "members.email_address,members.status,total_items",
                },
            )
            members = body.get("members", [])
            for m in members:
                e = (m.get("email_address") or "").strip().lower()
                if e:
                    emails.add(e)
            offset += len(members)
            total = body.get("total_items", 0)
            if not members or offset >= total:
                break
        return emails

    def suppressed_emails(self) -> set[str]:
        """Everyone Mailchimp will not deliver to: unsubscribed + cleaned. These
        must never be tagged for an auto-send (precedence rule 5)."""
        out: set[str] = set()
        for status in ("unsubscribed", "cleaned"):
            out |= self.list_emails_by_status(status)
        return out

    # ---- write (caller must gate behind --test/--live) -----------------
    def upsert_member(
        self, email: str, first_name: str = "", status_if_new: str = "subscribed"
    ) -> dict:
        """Create or update a member. PUT is idempotent on the email hash."""
        payload = {
            "email_address": email,
            "status_if_new": status_if_new,
            "merge_fields": {},
        }
        if first_name:
            payload["merge_fields"]["FNAME"] = first_name
        return self._request(
            "PUT",
            f"/lists/{self.s.audience_id}/members/{subscriber_hash(email)}",
            json=payload,
        )

    def add_tag(self, email: str, tag: str) -> dict:
        return self._request(
            "POST",
            f"/lists/{self.s.audience_id}/members/{subscriber_hash(email)}/tags",
            json={"tags": [{"name": tag, "status": "active"}]},
        )

    def remove_tag(self, email: str, tag: str) -> dict:
        return self._request(
            "POST",
            f"/lists/{self.s.audience_id}/members/{subscriber_hash(email)}/tags",
            json={"tags": [{"name": tag, "status": "inactive"}]},
        )

    def tag_member(self, email: str, first_name: str, tag: str) -> dict:
        """Upsert then tag — the full 'mark this climber for nudge X' op."""
        self.upsert_member(email, first_name)
        return self.add_tag(email, tag)

    def archive_member(self, email: str) -> dict:
        """DELETE a member -> Mailchimp archives them (removes from the active
        audience, keeps history). Used by `cleanup` to clear test dummies."""
        return self._request(
            "DELETE",
            f"/lists/{self.s.audience_id}/members/{subscriber_hash(email)}",
        )
