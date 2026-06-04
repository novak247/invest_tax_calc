from __future__ import annotations

import unittest

from invest_tax_calc.t212 import parse_trading212_csv


class Trading212ParserTest(unittest.TestCase):
    def test_parses_basic_buy_and_sell_rows(self) -> None:
        csv_text = """Action,Time,Ticker,ISIN,No. of shares,Price / share,Currency (Price / share),Total,Currency (Total),ID
Market buy,2025-01-01 10:00:00,VUAA,IE00BFMXXD54,2,100,EUR,200,EUR,buy-1
Deposit,2025-01-02 10:00:00,,,,,,,,
Market sell,2025-06-01 10:00:00,VUAA,IE00BFMXXD54,1,120,EUR,120,EUR,sell-1
"""

        trades = parse_trading212_csv(csv_text, default_currency="EUR")

        self.assertEqual(len(trades), 2)
        self.assertEqual(trades[0].kind, "buy")
        self.assertEqual(trades[0].instrument_key, "ISIN:IE00BFMXXD54")
        self.assertEqual(trades[1].kind, "sell")
        self.assertEqual(trades[1].gross.currency, "EUR")


if __name__ == "__main__":
    unittest.main()
