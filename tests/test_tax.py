from __future__ import annotations

import unittest
from datetime import datetime
from decimal import Decimal

from invest_tax_calc.models import Money, Trade
from invest_tax_calc.tax import (
    analyze_transactions,
    plan_batch,
    plan_sale,
    plan_target_proceeds,
)


def trade(
    kind: str,
    when: str,
    qty: str,
    gross: str,
    source_id: str,
    instrument_key: str = "ISIN:IE00TEST",
    ticker: str = "ETF",
) -> Trade:
    return Trade(
        source="test",
        source_id=source_id,
        action=kind,
        kind=kind,
        traded_at=datetime.fromisoformat(when),
        instrument_key=instrument_key,
        ticker=ticker,
        isin=instrument_key.removeprefix("ISIN:"),
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

    def test_batch_plan_evaluates_multiple_instruments_as_one_scenario(self) -> None:
        txs = [
            trade("buy", "2025-01-01T10:00:00", "10", "40000", "a-buy", "ISIN:A", "AAA"),
            trade("buy", "2025-01-01T10:00:00", "10", "40000", "b-buy", "ISIN:B", "BBB"),
        ]

        result = plan_batch(
            txs,
            rates={},
            sale_date="2025-07-01",
            rows=[
                {
                    "instrumentKey": "ISIN:A",
                    "quantity": "10",
                    "pricePerShareCzk": "6000",
                },
                {
                    "instrumentKey": "ISIN:B",
                    "quantity": "10",
                    "pricePerShareCzk": "6000",
                },
            ],
        )

        self.assertEqual(result["estimatedProceedsCzk"], 120000.0)
        self.assertFalse(result["afterSummary"]["grossLimitApplies"])
        self.assertEqual(result["taxableGainDeltaCzk"], 40000.0)
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(len(result["lotGroups"]), 2)

    def test_target_optimizer_uses_time_exempt_lots_first(self) -> None:
        txs = [
            trade("buy", "2020-01-01T10:00:00", "2", "100000", "old", "ISIN:OLD", "OLD"),
            trade("buy", "2025-01-01T10:00:00", "2", "100000", "new", "ISIN:NEW", "NEW"),
        ]

        result = plan_target_proceeds(
            txs,
            rates={},
            target_proceeds_czk="150000",
            sale_date="2026-06-01",
            optimization_mode="min_tax",
            candidate_instrument_keys=[],
            quotes={
                "ISIN:OLD": {"pricePerShareCzk": "100000"},
                "ISIN:NEW": {"pricePerShareCzk": "100000"},
            },
        )

        self.assertTrue(result["optimizer"]["targetReached"])
        self.assertEqual(result["estimatedProceedsCzk"], 150000.0)
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["instrumentKey"], "ISIN:OLD")
        self.assertEqual(result["rows"][0]["quantity"], 1.5)
        self.assertEqual(result["estimatedTaxDelta15Czk"], 0.0)

    def test_target_optimizer_uses_partial_lot_to_hit_target(self) -> None:
        result = plan_target_proceeds(
            [trade("buy", "2020-01-01T10:00:00", "10", "500", "old")],
            rates={},
            target_proceeds_czk="250",
            sale_date="2026-06-01",
            optimization_mode="min_tax",
            candidate_instrument_keys=[],
            quotes={"ISIN:IE00TEST": {"pricePerShareCzk": "100"}},
        )

        self.assertEqual(result["estimatedProceedsCzk"], 250.0)
        self.assertEqual(result["shortfallCzk"], 0.0)
        self.assertEqual(result["rows"][0]["quantity"], 2.5)
        self.assertEqual(result["rows"][0]["remainingQuantity"], 7.5)

    def test_target_optimizer_requires_price_for_each_candidate(self) -> None:
        with self.assertRaisesRegex(ValueError, "Enter or fetch a CZK price"):
            plan_target_proceeds(
                [trade("buy", "2020-01-01T10:00:00", "10", "500", "old")],
                rates={},
                target_proceeds_czk="250",
                sale_date="2026-06-01",
                optimization_mode="min_tax",
                candidate_instrument_keys=[],
                quotes={},
            )


if __name__ == "__main__":
    unittest.main()
