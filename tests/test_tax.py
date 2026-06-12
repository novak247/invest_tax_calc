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

    def test_planned_sale_groups_lots_by_tax_status_and_date_range(self) -> None:
        txs = [
            trade("buy", "2021-01-01T10:00:00", "5", "25000", "old"),
            trade("buy", "2024-01-01T10:00:00", "10", "50000", "new-1"),
            trade("buy", "2024-02-01T10:00:00", "10", "60000", "new-2"),
        ]
        result = plan_sale(
            txs,
            rates={},
            instrument_key="ISIN:IE00TEST",
            quantity="20",
            price_per_share_czk="10000",
            sale_date="2025-07-01",
        )

        groups = result["plannedMatchGroups"]

        self.assertEqual(len(groups), 2)
        self.assertFalse(groups[0]["taxable"])
        self.assertEqual(groups[0]["buyDateStart"], "2021-01-01")
        self.assertEqual(groups[0]["buyDateEnd"], "2021-01-01")
        self.assertEqual(groups[0]["lotCount"], 1)
        self.assertEqual(groups[0]["quantity"], 5.0)
        self.assertTrue(groups[1]["taxable"])
        self.assertEqual(groups[1]["buyDateStart"], "2024-01-01")
        self.assertEqual(groups[1]["buyDateEnd"], "2024-02-01")
        self.assertEqual(groups[1]["lotCount"], 2)
        self.assertEqual(groups[1]["quantity"], 15.0)


class AsOfCutoffTest(unittest.TestCase):
    def test_future_buy_is_excluded_from_holdings(self) -> None:
        result = analyze_transactions(
            [
                trade("buy", "2025-01-01T10:00:00", "10", "50000", "b1"),
                trade("buy", "2026-12-01T10:00:00", "10", "50000", "b-future"),
            ],
            rates={},
            as_of="2026-01-01",
        )

        self.assertEqual(result["buyCount"], 1)
        self.assertEqual(len(result["holdings"]), 1)
        self.assertEqual(result["holdings"][0]["quantity"], 10.0)
        self.assertEqual(len(result["holdings"][0]["lots"]), 1)

    def test_future_sell_does_not_affect_summary_or_warnings(self) -> None:
        result = analyze_transactions(
            [
                trade("buy", "2025-01-01T10:00:00", "10", "50000", "b1"),
                # 20 units sold while only 10 are held: would warn about an
                # unmatched sell if the future transaction leaked in.
                trade("sell", "2026-12-01T10:00:00", "20", "200000", "s-future"),
            ],
            rates={},
            tax_year=2026,
            as_of="2026-01-01",
        )

        self.assertEqual(result["sellCount"], 0)
        self.assertEqual(result["summary"]["grossProceedsCzk"], 0.0)
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["holdings"][0]["quantity"], 10.0)

    def test_real_unmatched_sell_before_as_of_still_warns(self) -> None:
        result = analyze_transactions(
            [trade("sell", "2025-06-01T10:00:00", "5", "20000", "s1")],
            rates={},
            tax_year=2025,
            as_of="2025-12-31",
        )

        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("without a matching buy lot", result["warnings"][0])


class PlanSaleOversellTest(unittest.TestCase):
    def buy_ten(self) -> list:
        return [trade("buy", "2025-01-01T10:00:00", "10", "50000", "b1")]

    def plan(self, txs: list, quantity: str, sale_date: str = "2025-06-01") -> dict:
        return plan_sale(
            txs,
            rates={},
            instrument_key="ISIN:IE00TEST",
            quantity=quantity,
            price_per_share_czk="6000",
            sale_date=sale_date,
        )

    def test_oversell_is_rejected_with_clear_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.plan(self.buy_ten(), "15")

        message = str(ctx.exception)
        self.assertIn("ETF", message)
        self.assertIn("15", message)
        self.assertIn("10", message)

    def test_sale_before_purchase_is_rejected(self) -> None:
        txs = [trade("buy", "2025-06-01T10:00:00", "10", "50000", "b1")]
        with self.assertRaises(ValueError):
            self.plan(txs, "5", sale_date="2025-01-01")

    def test_exact_full_position_sale_succeeds(self) -> None:
        result = self.plan(self.buy_ten(), "10")
        self.assertEqual(result["plannedProceedsCzk"], 60000.0)

    def test_tiny_rounding_overshoot_is_tolerated(self) -> None:
        result = self.plan(self.buy_ten(), "10.000000005")
        self.assertEqual(result["plannedProceedsCzk"], 60000.0)

    def test_overshoot_beyond_epsilon_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.plan(self.buy_ten(), "10.0000001")


if __name__ == "__main__":
    unittest.main()
