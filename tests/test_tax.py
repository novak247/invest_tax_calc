from __future__ import annotations

import unittest
from datetime import datetime
from decimal import Decimal

from invest_tax_calc.models import Money, Trade
from invest_tax_calc.tax import analyze_transactions, plan_sale


def trade(kind: str, when: str, qty: str, gross: str, source_id: str) -> Trade:
    return Trade(
        source="test",
        source_id=source_id,
        action=kind,
        kind=kind,
        traded_at=datetime.fromisoformat(when),
        instrument_key="ISIN:IE00TEST",
        ticker="ETF",
        isin="IE00TEST",
        quantity=Decimal(qty),
        gross=Money(Decimal(gross), "CZK"),
    )


class TaxEngineTest(unittest.TestCase):
    def test_gross_limit_exempts_short_term_sales(self) -> None:
        result = analyze_transactions(
            [
                trade("buy", "2025-01-01T10:00:00", "10", "50000", "b1"),
                trade("sell", "2025-06-01T10:00:00", "10", "90000", "s1"),
            ],
            rates={},
            tax_year=2025,
            as_of="2025-12-31",
        )

        self.assertTrue(result["summary"]["grossLimitApplies"])
        self.assertEqual(result["summary"]["taxableGainCzk"], 0.0)

    def test_crossing_gross_limit_makes_short_term_sales_taxable(self) -> None:
        result = analyze_transactions(
            [
                trade("buy", "2025-01-01T10:00:00", "20", "80000", "b1"),
                trade("sell", "2025-06-01T10:00:00", "10", "60000", "s1"),
                trade("sell", "2025-07-01T10:00:00", "10", "60000", "s2"),
            ],
            rates={},
            tax_year=2025,
            as_of="2025-12-31",
        )

        self.assertFalse(result["summary"]["grossLimitApplies"])
        self.assertEqual(result["summary"]["taxableGainCzk"], 40000.0)
        self.assertEqual(result["summary"]["estimatedTax15Czk"], 6000.0)

    def test_time_test_exempts_lot_after_three_years(self) -> None:
        result = analyze_transactions(
            [
                trade("buy", "2021-01-01T10:00:00", "10", "50000", "b1"),
                trade("sell", "2025-06-01T10:00:00", "10", "200000", "s1"),
            ],
            rates={},
            tax_year=2025,
            as_of="2025-12-31",
        )

        self.assertFalse(result["summary"]["grossLimitApplies"])
        self.assertEqual(result["summary"]["timeExemptProceedsCzk"], 200000.0)
        self.assertEqual(result["summary"]["taxableGainCzk"], 0.0)

    def test_planned_sale_delta_can_remove_gross_limit_exemption(self) -> None:
        txs = [
            trade("buy", "2025-01-01T10:00:00", "20", "80000", "b1"),
            trade("sell", "2025-06-01T10:00:00", "10", "60000", "s1"),
        ]
        result = plan_sale(
            txs,
            rates={},
            instrument_key="ISIN:IE00TEST",
            quantity="10",
            price_per_share_czk="6000",
            sale_date="2025-07-01",
        )

        self.assertEqual(result["plannedProceedsCzk"], 60000.0)
        self.assertEqual(result["deltaTaxableGainCzk"], 40000.0)
        self.assertEqual(result["deltaEstimatedTax15Czk"], 6000.0)


if __name__ == "__main__":
    unittest.main()
