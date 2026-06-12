from __future__ import annotations

import unittest
from datetime import datetime
from decimal import Decimal

from invest_tax_calc.models import Money, Trade
from invest_tax_calc.opportunities import compare_sale_dates, tax_opportunities

KEY = "ISIN:IE00TEST"
OTHER = "ISIN:IE00OTHER"


def trade(
    kind: str,
    when: str,
    qty: str,
    gross: str,
    source_id: str,
    key: str = KEY,
    ticker: str = "ETF",
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


class TaxOpportunitiesTest(unittest.TestCase):
    def test_remaining_allowance_below_threshold(self) -> None:
        result = tax_opportunities(
            [
                trade("buy", "2025-01-01T10:00:00", "10", "50000", "b1"),
                trade("sell", "2025-06-01T10:00:00", "5", "40000", "s1"),
            ],
            rates={},
            tax_year=2025,
            as_of="2025-12-31",
        )

        self.assertEqual(result["selectedYear"], 2025)
        self.assertEqual(result["existingGrossProceedsCzk"], 40000.0)
        self.assertEqual(result["remainingGrossAllowanceCzk"], 60000.0)
        self.assertFalse(result["grossLimitCrossed"])
        self.assertIn("100,000", result["grossLimitWarning"])

    def test_zero_allowance_after_crossing_threshold(self) -> None:
        result = tax_opportunities(
            [
                trade("buy", "2025-01-01T10:00:00", "20", "100000", "b1"),
                trade("sell", "2025-06-01T10:00:00", "10", "120000", "s1"),
            ],
            rates={},
            tax_year=2025,
            as_of="2025-12-31",
        )

        self.assertEqual(result["existingGrossProceedsCzk"], 120000.0)
        self.assertEqual(result["remainingGrossAllowanceCzk"], 0.0)
        self.assertTrue(result["grossLimitCrossed"])

    def test_milestones_group_by_date_and_instrument(self) -> None:
        result = tax_opportunities(
            [
                # Two same-day lots merge into one milestone.
                trade("buy", "2024-03-01T09:00:00", "3", "15000", "b1"),
                trade("buy", "2024-03-01T15:00:00", "2", "10000", "b2"),
                trade("buy", "2024-06-01T10:00:00", "4", "24000", "b3"),
                trade("buy", "2024-03-01T10:00:00", "7", "70000", "b4", OTHER, "XY7D"),
            ],
            rates={},
            as_of="2025-01-01",
        )

        milestones = result["milestones"]
        self.assertEqual(len(milestones), 3)
        self.assertEqual(
            [item["taxFreeDate"] for item in milestones],
            ["2027-03-01", "2027-03-01", "2027-06-01"],
        )

        first = milestones[0]
        self.assertEqual(first["ticker"], "ETF")
        self.assertEqual(first["quantity"], 5.0)
        self.assertEqual(first["cumulativeTaxFreeQuantity"], 5.0)
        self.assertEqual(first["costCzk"], 25000.0)

        self.assertEqual(milestones[1]["ticker"], "XY7D")
        self.assertEqual(milestones[1]["quantity"], 7.0)

        last = milestones[2]
        self.assertEqual(last["quantity"], 4.0)
        self.assertEqual(last["cumulativeTaxFreeQuantity"], 9.0)

    def test_past_milestones_are_excluded_and_count_into_cumulative(self) -> None:
        result = tax_opportunities(
            [
                trade("buy", "2021-01-01T10:00:00", "6", "30000", "old"),
                trade("buy", "2024-06-01T10:00:00", "4", "24000", "new"),
            ],
            rates={},
            as_of="2025-01-01",
        )

        milestones = result["milestones"]
        self.assertEqual(len(milestones), 1)
        self.assertEqual(milestones[0]["taxFreeDate"], "2027-06-01")
        self.assertEqual(milestones[0]["quantity"], 4.0)
        # The 2021 lot already passed the time test and feeds the running total.
        self.assertEqual(milestones[0]["cumulativeTaxFreeQuantity"], 10.0)

    def test_future_purchases_are_excluded_by_as_of(self) -> None:
        result = tax_opportunities(
            [
                trade("buy", "2024-06-01T10:00:00", "4", "24000", "b1"),
                trade("buy", "2026-12-01T10:00:00", "100", "500000", "b-future"),
            ],
            rates={},
            as_of="2026-01-01",
        )

        milestones = result["milestones"]
        self.assertEqual(len(milestones), 1)
        self.assertEqual(milestones[0]["quantity"], 4.0)
        self.assertEqual(result["holdingsCount"], 1)

    def test_leap_day_acquisition_milestone_date(self) -> None:
        result = tax_opportunities(
            [trade("buy", "2024-02-29T10:00:00", "1", "5000", "leap")],
            rates={},
            as_of="2025-01-01",
        )

        self.assertEqual(result["milestones"][0]["taxFreeDate"], "2027-02-28")

    def test_empty_transactions_raise(self) -> None:
        with self.assertRaises(ValueError):
            tax_opportunities([], rates={}, as_of="2025-01-01")


class CompareSaleDatesTest(unittest.TestCase):
    def test_waiting_until_time_exempt_reduces_tax(self) -> None:
        txs = [trade("buy", "2023-01-10T10:00:00", "10", "50000", "b1")]
        result = compare_sale_dates(
            txs,
            rates={},
            instrument_key=KEY,
            quantity="10",
            price_per_share_czk="15000",
            first_date="2025-06-01",
        )

        # Second date defaults to the next tax-free date of the instrument.
        self.assertEqual(result["second"]["saleDate"], "2026-01-10")
        # 150k proceeds cross the gross limit; on the first date the lot is
        # short-term so the 100k gain is taxed, on the second it is time-exempt.
        self.assertEqual(result["first"]["estimatedProceedsCzk"], 150000.0)
        self.assertEqual(result["first"]["taxableProceedsCzk"], 150000.0)
        self.assertEqual(result["first"]["estimatedTaxDelta15Czk"], 15000.0)
        self.assertEqual(result["second"]["exemptProceedsCzk"], 150000.0)
        self.assertEqual(result["second"]["estimatedTaxDelta15Czk"], 0.0)
        self.assertEqual(result["comparison"]["estimatedTaxDifference15Czk"], -15000.0)
        self.assertEqual(result["comparison"]["taxableGainDifferenceCzk"], -100000.0)

    def test_rejects_oversell_on_first_date(self) -> None:
        txs = [trade("buy", "2025-09-01T10:00:00", "10", "50000", "b1")]
        with self.assertRaises(ValueError):
            compare_sale_dates(
                txs,
                rates={},
                instrument_key=KEY,
                quantity="5",
                price_per_share_czk="6000",
                first_date="2025-06-01",
                second_date="2026-06-01",
            )

    def test_rejects_oversell_on_second_date(self) -> None:
        txs = [
            trade("buy", "2023-01-10T10:00:00", "10", "50000", "b1"),
            # A real sale between the two dates shrinks the second-date holding.
            trade("sell", "2025-08-01T10:00:00", "6", "36000", "s1"),
        ]
        with self.assertRaises(ValueError):
            compare_sale_dates(
                txs,
                rates={},
                instrument_key=KEY,
                quantity="8",
                price_per_share_czk="6000",
                first_date="2025-06-01",
                second_date="2026-02-01",
            )

    def test_default_second_date_requires_an_immature_lot(self) -> None:
        txs = [trade("buy", "2020-01-01T10:00:00", "10", "50000", "b1")]
        with self.assertRaises(ValueError):
            compare_sale_dates(
                txs,
                rates={},
                instrument_key=KEY,
                quantity="1",
                price_per_share_czk="6000",
                first_date="2025-06-01",
            )


if __name__ == "__main__":
    unittest.main()
