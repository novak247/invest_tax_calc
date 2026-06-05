from __future__ import annotations

import unittest
from decimal import Decimal

from invest_tax_calc.t212_pdf import parse_trading212_pdf_text


class Trading212PdfParserTest(unittest.TestCase):
    def test_parses_activity_statement_buy_rows(self) -> None:
        text = """
Account Invest - executed trades
2026-06-03 07:00:09 ASML NL0010273215 EUR 52020792362 Koupit 0.00275838 1496 4.1265 Market OTC Regular hours 0.04132735 CZK 0.15 - - 100
2026-06-03 07:04:09 VUAA IE00BFMXXD54 EUR 52023164031 Koupit 0.03265828 126.35 4.1264 Market OTC Regular hours 0.04132572 CZK 0.15 - - 100
"""

        trades = parse_trading212_pdf_text(text, filename="statement.pdf")

        self.assertEqual(len(trades), 2)
        self.assertEqual(trades[0].kind, "buy")
        self.assertEqual(trades[0].source_id, "52020792362")
        self.assertEqual(trades[0].ticker, "ASML")
        self.assertEqual(trades[0].isin, "NL0010273215")
        self.assertEqual(trades[0].quantity, Decimal("0.00275838"))
        self.assertEqual(trades[0].gross.amount, Decimal("100"))
        self.assertEqual(trades[0].gross.currency, "CZK")
        self.assertEqual(trades[0].fees[0].amount, Decimal("0.15"))

    def test_parses_rows_with_glued_date_and_time(self) -> None:
        # Older statements extract the timestamp without a space between the
        # date and time, e.g. "2024-08-0212:02:20".
        text = """
2024-08-0212:02:20 ASML NL0010273215 EUR 18400131318 18400131320 Koupit 0.287684 57 16.398 Market OTC Regular hours 0.03957 CZK 0.62 - - 415
"""

        trades = parse_trading212_pdf_text(text, filename="statement.pdf")

        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].kind, "buy")
        self.assertEqual(trades[0].source_id, "18400131320")
        self.assertEqual(trades[0].ticker, "ASML")
        self.assertEqual(trades[0].quantity, Decimal("0.287684"))
        self.assertEqual(trades[0].traded_at.isoformat(), "2024-08-02T12:02:20")
        self.assertEqual(trades[0].gross.amount, Decimal("415"))
        self.assertEqual(trades[0].gross.currency, "CZK")

    def test_parses_rows_with_parent_and_execution_ids(self) -> None:
        text = """
2025-08-25 07:03:40 XY7D IE0002L5QB31 EUR 37607775795 37651033009 Prodat 1.45785983 12.7 18.5148 Market OTC Regular hours 0.04078513 CZK 0.68 - -3.94 453.28
"""

        trades = parse_trading212_pdf_text(text, filename="statement.pdf")

        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].kind, "sell")
        self.assertEqual(trades[0].source_id, "37651033009")
        self.assertEqual(trades[0].quantity, Decimal("1.45785983"))
        self.assertEqual(trades[0].gross.amount, Decimal("453.28"))
        self.assertEqual(trades[0].gross.currency, "CZK")
        self.assertEqual(trades[0].fees[0].amount, Decimal("0.68"))


if __name__ == "__main__":
    unittest.main()
