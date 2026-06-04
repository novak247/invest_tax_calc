from __future__ import annotations

import unittest

from app import AppError, _csv_payload, _resolve_project_path
from invest_tax_calc.email_import.providers import Attachment


class AppGmailTests(unittest.TestCase):
    def test_import_paths_must_stay_inside_project(self) -> None:
        with self.assertRaises(AppError):
            _resolve_project_path("..")

    def test_csv_payload_decodes_attachment_for_browser_handoff(self) -> None:
        payload = _csv_payload(
            Attachment(
                provider="gmail",
                message_id="message-1",
                filename="Trading 212 report.csv",
                data=b"Action,Time\nMarket buy,2025-01-01\n",
                content_type="text/csv",
            ),
            _resolve_project_path("imports/email_reports/gmail/report.csv"),
        )

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["filename"], "Trading 212 report.csv")
        self.assertIn("Market buy", payload["content"])
        self.assertFalse(payload["tooLarge"])


if __name__ == "__main__":
    unittest.main()
