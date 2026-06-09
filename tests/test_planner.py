from __future__ import annotations

import unittest
from datetime import datetime
from decimal import Decimal

from invest_tax_calc.models import Money, Trade
from invest_tax_calc.planner import plan_sale_batch, plan_target_proceeds

OLD = "ISIN:IE00OLD"
NEW = "ISIN:IE00NEW"


def trade(
    kind: str,
    when: str,
    qty: str,
    gross: str,
    source_id: str,
    key: str = OLD,
    ticker: str = "OLD",
) -> Trade:
    return Trade(
        source="test",
        source_id=source_id,
        action=kind,
        kind=kind,
        traded_at=datetime.fromisoformat(when),
        instrument_key=key,
        ticker=ticker,
        isin=key.removeprefix("ISIN:"),
        quantity=Decimal(qty),
        gross=Money(Decimal(gross), "CZK"),
    )


def two_instrument_portfolio() -> list[Trade]:
    """OLD passed the 3-year time test; NEW did not. Both 20 units at 5000 CZK."""
    return [
        trade("buy", "2021-01-01T10:00:00", "20", "100000", "b-old", OLD, "OLD"),
        trade("buy", "2025-01-01T10:00:00", "20", "100000", "b-new", NEW, "NEW"),
    ]


class PlanSaleBatchTest(unittest.TestCase):
    def test_combined_rows_cross_gross_limit_and_become_taxable(self) -> None:
        txs = [
            trade("buy", "2025-01-01T10:00:00", "10", "40000", "b1", OLD, "OLD"),
            trade("buy", "2025-02-01T10:00:00", "10", "40000", "b2", NEW, "NEW"),
        ]
        result = plan_sale_batch(
            txs,
            rates={},
            sale_date="2025-07-01",
            rows=[
                {"instrumentKey": OLD, "quantity": "10", "pricePerShareCzk": "6000"},
                {"instrumentKey": NEW, "quantity": "10", "pricePerShareCzk": "6000"},
            ],
        )

        # Each row alone (60k) stays under the 100k gross limit, together they
        # cross it, so both planned sales must come back taxable.
        self.assertEqual(result["estimatedProceedsCzk"], 120000.0)
        self.assertFalse(result["afterSummary"]["grossLimitApplies"])
        self.assertEqual(result["taxableGainDeltaCzk"], 40000.0)
        self.assertEqual(result["estimatedTaxDelta15Czk"], 6000.0)
        self.assertEqual(len(result["rows"]), 2)
        for row in result["rows"]:
            self.assertEqual(row["taxableGainCzk"], 20000.0)

    def test_lot_groups_stay_compact_per_instrument_and_status(self) -> None:
        txs = [
            trade("buy", "2024-01-01T10:00:00", "5", "25000", "n1", NEW, "NEW"),
            trade("buy", "2024-02-01T10:00:00", "5", "25000", "n2", NEW, "NEW"),
            trade("buy", "2024-03-01T10:00:00", "5", "25000", "n3", NEW, "NEW"),
        ]
        result = plan_sale_batch(
            txs,
            rates={},
            sale_date="2025-07-01",
            rows=[{"instrumentKey": NEW, "quantity": "15", "pricePerShareCzk": "10000"}],
        )

        groups = result["lotGroups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["lotCount"], 3)
        self.assertEqual(groups[0]["buyDateStart"], "2024-01-01")
        self.assertEqual(groups[0]["buyDateEnd"], "2024-03-01")
        self.assertEqual(groups[0]["quantity"], 15.0)

    def test_rejects_row_without_price(self) -> None:
        txs = two_instrument_portfolio()
        with self.assertRaises(ValueError):
            plan_sale_batch(
                txs,
                rates={},
                sale_date="2025-07-01",
                rows=[{"instrumentKey": OLD, "quantity": "1", "pricePerShareCzk": ""}],
            )

    def test_rejects_empty_rows(self) -> None:
        with self.assertRaises(ValueError):
            plan_sale_batch(
                two_instrument_portfolio(), rates={}, sale_date="2025-07-01", rows=[]
            )


class PlanTargetProceedsTest(unittest.TestCase):
    def quotes(self) -> dict[str, dict[str, str]]:
        return {
            OLD: {"pricePerShareCzk": "10000"},
            NEW: {"pricePerShareCzk": "10000"},
        }

    def test_min_tax_uses_time_exempt_lots_first(self) -> None:
        result = plan_target_proceeds(
            two_instrument_portfolio(),
            rates={},
            sale_date="2025-07-01",
            target_proceeds_czk="150000",
            optimization_mode="min_tax",
            quotes=self.quotes(),
        )

        self.assertTrue(result["optimizer"]["targetReached"])
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["instrumentKey"], OLD)
        self.assertEqual(result["rows"][0]["quantity"], 15.0)
        self.assertEqual(result["estimatedProceedsCzk"], 150000.0)
        self.assertEqual(result["taxableGainDeltaCzk"], 0.0)
        self.assertEqual(result["estimatedTaxDelta15Czk"], 0.0)

    def test_preserve_tax_free_sells_taxable_lots_first(self) -> None:
        result = plan_target_proceeds(
            two_instrument_portfolio(),
            rates={},
            sale_date="2025-07-01",
            target_proceeds_czk="150000",
            optimization_mode="preserve_tax_free",
            quotes=self.quotes(),
        )

        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["instrumentKey"], NEW)
        # 15 units of NEW at 10000 with 5000 cost: 75000 gain, taxable.
        self.assertEqual(result["taxableGainDeltaCzk"], 75000.0)
        self.assertEqual(result["estimatedTaxDelta15Czk"], 11250.0)

    def test_partial_lot_hits_target_exactly(self) -> None:
        txs = [trade("buy", "2021-01-01T10:00:00", "20", "100000", "b1", OLD, "OLD")]
        result = plan_target_proceeds(
            txs,
            rates={},
            sale_date="2025-07-01",
            target_proceeds_czk="50000",
            optimization_mode="min_tax",
            quotes={OLD: {"pricePerShareCzk": "10000"}},
        )

        self.assertTrue(result["optimizer"]["targetReached"])
        self.assertEqual(result["rows"][0]["quantity"], 5.0)
        self.assertEqual(result["estimatedProceedsCzk"], 50000.0)
        self.assertEqual(result["shortfallCzk"], 0.0)
        self.assertEqual(result["rows"][0]["remainingQuantity"], 15.0)

    def test_unreachable_target_reports_shortfall(self) -> None:
        result = plan_target_proceeds(
            two_instrument_portfolio(),
            rates={},
            sale_date="2025-07-01",
            target_proceeds_czk="1000000",
            optimization_mode="min_tax",
            quotes=self.quotes(),
        )

        self.assertFalse(result["optimizer"]["targetReached"])
        self.assertEqual(result["estimatedProceedsCzk"], 400000.0)
        self.assertEqual(result["shortfallCzk"], 600000.0)
        self.assertTrue(any("target" in warning for warning in result["warnings"]))

    def test_instrument_without_quote_is_excluded_with_warning(self) -> None:
        result = plan_target_proceeds(
            two_instrument_portfolio(),
            rates={},
            sale_date="2025-07-01",
            target_proceeds_czk="100000",
            optimization_mode="min_tax",
            quotes={OLD: {"pricePerShareCzk": "10000"}},
        )

        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["instrumentKey"], OLD)
        self.assertTrue(any("excluded" in warning for warning in result["warnings"]))

    def test_no_quotes_at_all_raises(self) -> None:
        with self.assertRaises(ValueError):
            plan_target_proceeds(
                two_instrument_portfolio(),
                rates={},
                sale_date="2025-07-01",
                target_proceeds_czk="100000",
                optimization_mode="min_tax",
                quotes={},
            )

    def test_unknown_mode_raises(self) -> None:
        with self.assertRaises(ValueError):
            plan_target_proceeds(
                two_instrument_portfolio(),
                rates={},
                sale_date="2025-07-01",
                target_proceeds_czk="100000",
                optimization_mode="moon_shot",
                quotes=self.quotes(),
            )

    def test_lots_within_instrument_are_consumed_fifo(self) -> None:
        txs = [
            trade("buy", "2024-01-01T10:00:00", "10", "10000", "cheap", NEW, "NEW"),
            trade("buy", "2024-06-01T10:00:00", "10", "90000", "dear", NEW, "NEW"),
        ]
        result = plan_target_proceeds(
            txs,
            rates={},
            sale_date="2025-07-01",
            target_proceeds_czk="150000",
            optimization_mode="min_taxable_gain",
            quotes={NEW: {"pricePerShareCzk": "10000"}},
        )

        groups = result["lotGroups"]
        # The expensive (low-gain) lot cannot jump the queue: FIFO consumes the
        # cheap 2024-01 lot before the 2024-06 lot.
        self.assertEqual(groups[0]["buyDateStart"], "2024-01-01")
        self.assertEqual(result["rows"][0]["quantity"], 15.0)


if __name__ == "__main__":
    unittest.main()
