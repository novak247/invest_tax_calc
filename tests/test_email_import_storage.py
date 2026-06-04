import tempfile
import unittest
from pathlib import Path

from invest_tax_calc.email_import.providers import Attachment
from invest_tax_calc.email_import.storage import AttachmentStore, ImportLedger


class AttachmentStoreTests(unittest.TestCase):
    def test_saves_attachment_and_deduplicates_by_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ledger = ImportLedger(root / "ledger.json")
            store = AttachmentStore(root / "reports", ledger)

            attachment = Attachment(
                provider="gmail",
                message_id="message-1",
                filename="../Trading 212 report.csv",
                data=b"csv bytes",
            )

            first = store.save_attachment(attachment)
            second = store.save_attachment(
                Attachment(
                    provider="gmail",
                    message_id="message-2",
                    filename="same-content.csv",
                    data=b"csv bytes",
                )
            )

            self.assertTrue(first.saved)
            self.assertFalse(second.saved)
            self.assertEqual(second.reason, "duplicate")
            self.assertEqual(first.sha256, second.sha256)
            self.assertTrue(first.path.exists())
            self.assertNotIn("..", first.path.parts)
            self.assertEqual(len(ledger.entries), 1)


if __name__ == "__main__":
    unittest.main()
