"""Read-only email attachment importers."""

from .providers import Attachment, GmailClient
from .scopes import GMAIL_READONLY_SCOPE
from .storage import AttachmentStore, ImportLedger

__all__ = [
    "Attachment",
    "AttachmentStore",
    "GMAIL_READONLY_SCOPE",
    "GmailClient",
    "ImportLedger",
]
