from __future__ import annotations

import argparse
from pathlib import Path

from .oauth import GMAIL_OAUTH, run_loopback_oauth
from .providers import GmailClient
from .storage import AttachmentStore, ImportLedger


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m invest_tax_calc.email_import",
        description="Import broker report attachments from a read-only Gmail connection.",
    )
    _add_common_args(parser)
    parser.add_argument(
        "--query",
        default="from:trading212 has:attachment",
        help="Gmail search query. Defaults to Trading 212 messages with attachments.",
    )

    args = parser.parse_args(argv)
    token = run_loopback_oauth(
        GMAIL_OAUTH,
        client_id=args.client_id,
        client_secret=args.client_secret,
        port=args.port,
        open_browser=not args.no_browser,
    )
    access_token = token.get("access_token")
    if not access_token:
        raise RuntimeError("OAuth token response did not include an access token.")

    ledger = ImportLedger(Path(args.ledger))
    store = AttachmentStore(Path(args.output), ledger)

    saved = 0
    skipped = 0
    client = GmailClient(access_token)
    attachments = client.iter_report_attachments(
        query=args.query,
        max_messages=args.max_messages,
    )

    for attachment in attachments:
        result = store.save_attachment(attachment)
        if result.saved:
            saved += 1
            print(f"Saved: {result.path}")
        else:
            skipped += 1
            print(f"Skipped duplicate: {result.path}")

    print(f"Done. Saved {saved}; skipped {skipped} duplicate(s).")
    return 0


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--client-id", default=None, help="OAuth client ID.")
    parser.add_argument(
        "--client-secret",
        default=None,
        help="Optional OAuth client secret for providers that require it.",
    )
    parser.add_argument(
        "--output",
        default="imports/email_reports",
        help="Directory for downloaded report attachments.",
    )
    parser.add_argument(
        "--ledger",
        default=".invest_tax_calc/email_import_ledger.json",
        help="JSON ledger used to skip duplicate attachments.",
    )
    parser.add_argument(
        "--max-messages",
        type=int,
        default=200,
        help="Maximum matching messages to inspect.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Local OAuth callback port. 0 picks a free port.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the OAuth URL without opening a browser.",
    )
