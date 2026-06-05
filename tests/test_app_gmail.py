from __future__ import annotations

import unittest

from app import (
    AppError,
    GmailImportSession,
    _gmail_session_payload,
    _parse_gmail_oauth_credentials,
    _pdf_payload,
    _resolve_project_path,
)
from invest_tax_calc.email_import.providers import Attachment


class AppGmailTests(unittest.TestCase):
    def test_import_paths_must_stay_inside_project(self) -> None:
        with self.assertRaises(AppError):
            _resolve_project_path("..")

    def test_pdf_payload_includes_browser_handoff_content(self) -> None:
        payload = _pdf_payload(
            Attachment(
                provider="gmail",
                message_id="message-1",
                filename="Trading 212 statement.pdf",
                data=b"%PDF-1.7",
                content_type="application/pdf",
            ),
            _resolve_project_path("imports/email_reports/gmail/report.pdf"),
        )

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["filename"], "Trading 212 statement.pdf")
        self.assertTrue(payload["parseable"])
        self.assertEqual(payload["contentBase64"], "JVBERi0xLjc=")

    def test_gmail_session_payload_includes_progress(self) -> None:
        session = GmailImportSession(
            state="state",
            code_verifier="verifier",
            client_id="client",
            client_secret=None,
            redirect_uri="http://127.0.0.1/callback",
            query="from:trading212",
            max_messages=500,
            output=_resolve_project_path("imports/email_reports"),
            ledger=_resolve_project_path(".invest_tax_calc/email_import_ledger.json"),
            progress_phase="downloading",
            total_messages=466,
            processed_messages=123,
            total_attachments=120,
            processed_attachments=120,
            saved=100,
            skipped=20,
        )

        payload = _gmail_session_payload(session)

        self.assertEqual(payload["progress"]["phase"], "downloading")
        self.assertEqual(payload["progress"]["totalMessages"], 466)
        self.assertEqual(payload["progress"]["processedMessages"], 123)
        self.assertEqual(payload["progress"]["saved"], 100)
        self.assertEqual(payload["progress"]["skipped"], 20)

    def test_google_installed_credentials_json_is_accepted(self) -> None:
        config = _parse_gmail_oauth_credentials(
            """
            {
              "installed": {
                "client_id": "client.apps.googleusercontent.com",
                "client_secret": "secret"
              }
            }
            """
        )

        self.assertEqual(config.client_id, "client.apps.googleusercontent.com")
        self.assertEqual(config.client_secret, "secret")
        self.assertEqual(config.source, "installed")

    def test_google_credentials_json_requires_client_id(self) -> None:
        with self.assertRaises(AppError):
            _parse_gmail_oauth_credentials('{"installed": {"client_secret": "secret"}}')


if __name__ == "__main__":
    unittest.main()
