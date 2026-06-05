from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .providers import Attachment


@dataclass(frozen=True)
class SaveResult:
    saved: bool
    sha256: str
    path: Path
    reason: str


class ImportLedger:
    def __init__(self, path: Path):
        self.path = path
        self.entries: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.entries = {}
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.entries = data.get("attachments", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "attachments": self.entries,
        }
        self.path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def has(self, sha256: str) -> bool:
        return sha256 in self.entries

    def get_path(self, sha256: str) -> Path | None:
        entry = self.entries.get(sha256)
        if not entry:
            return None
        return Path(entry["path"])

    def add(self, sha256: str, attachment: Attachment, path: Path) -> None:
        self.entries[sha256] = {
            "provider": attachment.provider,
            "message_id": attachment.message_id,
            "filename": attachment.filename,
            "sender": attachment.sender,
            "subject": attachment.subject,
            "received_at": attachment.received_at,
            "content_type": attachment.content_type,
            "path": str(path),
            "imported_at_utc": datetime.now(timezone.utc).isoformat(),
        }


class AttachmentStore:
    def __init__(self, output_dir: Path, ledger: ImportLedger):
        self.output_dir = output_dir
        self.ledger = ledger

    def save_attachment(self, attachment: Attachment, *, save_ledger: bool = True) -> SaveResult:
        digest = hashlib.sha256(attachment.data).hexdigest()
        existing_path = self.ledger.get_path(digest)
        if existing_path and existing_path.is_file():
            return SaveResult(
                saved=False,
                sha256=digest,
                path=existing_path,
                reason="duplicate",
            )

        provider_dir = self.output_dir / _safe_name(attachment.provider)
        provider_dir.mkdir(parents=True, exist_ok=True)

        filename = _safe_filename(attachment.filename)
        destination = _unique_destination(provider_dir / filename, digest)
        destination.write_bytes(attachment.data)

        self.ledger.add(digest, attachment, destination)
        if save_ledger:
            self.ledger.save()
        return SaveResult(
            saved=True,
            sha256=digest,
            path=destination,
            reason="saved",
        )


def _safe_filename(filename: str) -> str:
    name = Path(filename).name
    safe = _safe_name(name)
    if "." not in safe:
        return safe
    stem, suffix = safe.rsplit(".", 1)
    return f"{stem[:120]}.{suffix[:16]}"


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._() -]+", "_", value)
    safe = re.sub(r"\s+", " ", safe).strip(" ._")
    return safe or "attachment"


def _unique_destination(path: Path, digest: str) -> Path:
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    short_hash = digest[:10]
    candidate = path.with_name(f"{stem}-{short_hash}{suffix}")
    counter = 2
    while candidate.exists():
        candidate = path.with_name(f"{stem}-{short_hash}-{counter}{suffix}")
        counter += 1
    return candidate
