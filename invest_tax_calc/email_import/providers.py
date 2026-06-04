from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class Attachment:
    provider: str
    message_id: str
    filename: str
    data: bytes
    sender: str | None = None
    subject: str | None = None
    received_at: str | None = None
    content_type: str | None = None


class BearerSession:
    def __init__(self, access_token: str):
        self.access_token = access_token

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request_headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
        }
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(url, headers=request_headers, method="GET")
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))


class GmailClient:
    base_url = "https://gmail.googleapis.com/gmail/v1/users/me"

    def __init__(self, access_token: str):
        self.session = BearerSession(access_token)

    def iter_report_attachments(
        self,
        *,
        query: str = "from:trading212 has:attachment",
        max_messages: int = 200,
    ) -> Iterator[Attachment]:
        for message_id in self._iter_message_ids(query=query, max_messages=max_messages):
            message = self.session.get_json(
                f"{self.base_url}/messages/{message_id}",
                params={"format": "full"},
            )
            headers = _gmail_headers(message)
            for part in _walk_gmail_parts(message.get("payload", {})):
                filename = part.get("filename")
                if not filename:
                    continue

                body = part.get("body", {})
                data = self._gmail_part_bytes(message_id, body)
                if data is None:
                    continue

                yield Attachment(
                    provider="gmail",
                    message_id=message_id,
                    filename=filename,
                    data=data,
                    sender=headers.get("from"),
                    subject=headers.get("subject"),
                    received_at=headers.get("date"),
                    content_type=part.get("mimeType"),
                )

    def _iter_message_ids(self, *, query: str, max_messages: int) -> Iterator[str]:
        remaining = max_messages
        page_token: str | None = None

        while remaining > 0:
            params: dict[str, object] = {
                "q": query,
                "maxResults": min(remaining, 500),
            }
            if page_token:
                params["pageToken"] = page_token

            page = self.session.get_json(f"{self.base_url}/messages", params=params)
            for item in page.get("messages", []):
                if remaining <= 0:
                    return
                message_id = item.get("id")
                if message_id:
                    remaining -= 1
                    yield message_id

            page_token = page.get("nextPageToken")
            if not page_token:
                return

    def _gmail_part_bytes(self, message_id: str, body: dict) -> bytes | None:
        if body.get("data"):
            return _decode_base64url(body["data"])

        attachment_id = body.get("attachmentId")
        if not attachment_id:
            return None

        attachment = self.session.get_json(
            f"{self.base_url}/messages/{message_id}/attachments/{attachment_id}"
        )
        encoded = attachment.get("data")
        if not encoded:
            return None
        return _decode_base64url(encoded)


def _gmail_headers(message: dict) -> dict[str, str]:
    headers = message.get("payload", {}).get("headers", [])
    result: dict[str, str] = {}
    for header in headers:
        name = header.get("name")
        value = header.get("value")
        if name and value:
            result[name.lower()] = value
    return result


def _walk_gmail_parts(part: dict) -> Iterator[dict]:
    yield part
    for child in part.get("parts", []) or []:
        yield from _walk_gmail_parts(child)


def _decode_base64url(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}")

