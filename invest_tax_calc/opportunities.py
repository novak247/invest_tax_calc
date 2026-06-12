from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from .models import Trade
from .tax import (
    GROSS_PROCEEDS_EXEMPTION_CZK,
    _num,
    _parse_date,
    _qty,
    analyze_transactions,
    plan_sale,
)

GROSS_LIMIT_WARNING = (
    "Crossing the 100,000 CZK annual gross-proceeds limit can retroactively make "
    "earlier short-term sales in the same year taxable. This allowance covers the "
    "gross-proceeds rule only; sales exempt by the 3-year time test are a separate "
    "exemption and stay exempt above the limit."
)


def tax_opportunities(
    transactions: list[Trade],
    *,
    rates: dict[str, Decimal],
    tax_year: int | None = None,
    as_of: str | date | None = None,
) -> dict[str, Any]:
    """Summarize the remaining gross-proceeds allowance and upcoming
    tax-free (3-year time test) milestones for the open holdings."""
    if not transactions:
        raise ValueError("Load and analyze statements before reviewing tax opportunities.")

    analysis = analyze_transactions(
        transactions, rates=rates, tax_year=tax_year, as_of=as_of
    )
    summary = analysis["summary"]
    as_of_date = _parse_date(analysis["asOf"])

    gross = Decimal(str(summary["grossProceedsCzk"]))
    remaining = max(GROSS_PROCEEDS_EXEMPTION_CZK - gross, Decimal("0"))
    holdings = analysis.get("holdings", [])

    return {
        "selectedYear": analysis["selectedYear"],
        "asOf": analysis["asOf"],
        "grossLimitCzk": _num(GROSS_PROCEEDS_EXEMPTION_CZK),
        "existingGrossProceedsCzk": _num(gross),
        "remainingGrossAllowanceCzk": _num(remaining),
        "grossLimitCrossed": gross > GROSS_PROCEEDS_EXEMPTION_CZK,
        "grossLimitWarning": GROSS_LIMIT_WARNING,
        "milestones": _tax_free_milestones(holdings, as_of_date),
        "holdingsCount": len(holdings),
    }


def compare_sale_dates(
    transactions: list[Trade],
    *,
    rates: dict[str, Decimal],
    instrument_key: str,
    quantity: str,
    price_per_share_czk: str,
    first_date: str = "",
    second_date: str = "",
) -> dict[str, Any]:
    """Run the same planned sale on two dates and report the difference.

    The first date defaults to today; the second defaults to the instrument's
    next tax-free date. Both scenarios reuse the planner, so oversells and
    the global gross-proceeds rule are handled identically to a normal plan.
    """
    if not instrument_key:
        raise ValueError("Choose an instrument to compare.")

    first = _parse_date(str(first_date or "").strip() or date.today().isoformat())
    if str(second_date or "").strip():
        second = _parse_date(second_date)
    else:
        second = _next_tax_free_date(
            transactions, rates=rates, instrument_key=instrument_key, as_of=first
        )

    scenarios = [
        _scenario_payload(
            plan_sale(
                transactions,
                rates=rates,
                instrument_key=instrument_key,
                quantity=quantity,
                price_per_share_czk=price_per_share_czk,
                sale_date=when.isoformat(),
            )
        )
        for when in (first, second)
    ]
    first_scenario, second_scenario = scenarios

    def difference(key: str) -> float:
        return _num(
            Decimal(str(second_scenario[key])) - Decimal(str(first_scenario[key]))
        )

    return {
        "instrumentKey": instrument_key,
        "first": first_scenario,
        "second": second_scenario,
        "comparison": {
            "taxableGainDifferenceCzk": difference("taxableGainDeltaCzk"),
            "estimatedTaxDifference15Czk": difference("estimatedTaxDelta15Czk"),
            "exemptProceedsDifferenceCzk": difference("exemptProceedsCzk"),
            "taxableProceedsDifferenceCzk": difference("taxableProceedsCzk"),
            "grossProceedsAfterDifferenceCzk": difference("grossProceedsAfterCzk"),
        },
    }


def _scenario_payload(plan: dict[str, Any]) -> dict[str, Any]:
    exempt = Decimal("0")
    taxable = Decimal("0")
    for match in plan.get("plannedMatches", []):
        proceeds = Decimal(str(match.get("grossProceedsCzk") or "0"))
        if match.get("taxable"):
            taxable += proceeds
        else:
            exempt += proceeds

    return {
        "saleDate": plan["saleDate"],
        "estimatedProceedsCzk": plan["plannedProceedsCzk"],
        "taxableGainDeltaCzk": plan["deltaTaxableGainCzk"],
        "estimatedTaxDelta15Czk": plan["deltaEstimatedTax15Czk"],
        "exemptProceedsCzk": _num(exempt),
        "taxableProceedsCzk": _num(taxable),
        "grossProceedsAfterCzk": plan["afterSummary"]["grossProceedsCzk"],
        "plannedMatchGroups": plan.get("plannedMatchGroups", []),
        "warnings": plan.get("warnings", []),
    }


def _next_tax_free_date(
    transactions: list[Trade],
    *,
    rates: dict[str, Decimal],
    instrument_key: str,
    as_of: date,
) -> date:
    analysis = analyze_transactions(transactions, rates=rates, as_of=as_of)
    for holding in analysis.get("holdings", []):
        if holding.get("instrumentKey") != instrument_key:
            continue
        next_date = holding.get("nextTaxFreeDate")
        if not next_date:
            raise ValueError(
                f"All open lots of {holding.get('ticker') or instrument_key} already "
                "pass the 3-year time test. Pick the second sale date manually."
            )
        return _parse_date(str(next_date))
    raise ValueError(f"No open holding found for {instrument_key} on {as_of.isoformat()}.")


def _tax_free_milestones(
    holdings: list[dict[str, Any]], as_of: date
) -> list[dict[str, Any]]:
    """Group open lots by the exact date their 3-year time test passes."""
    milestones: list[dict[str, Any]] = []
    for holding in holdings:
        grouped: dict[str, dict[str, Decimal]] = {}
        for lot in holding.get("lots", []):
            tax_free = str(lot.get("taxFreeDate") or "")
            if not tax_free or _parse_date(tax_free) <= as_of:
                continue
            bucket = grouped.setdefault(
                tax_free, {"quantity": Decimal("0"), "cost": Decimal("0")}
            )
            bucket["quantity"] += Decimal(str(lot.get("quantity") or "0"))
            bucket["cost"] += Decimal(str(lot.get("costCzk") or "0"))

        cumulative = Decimal(str(holding.get("taxFreeQuantityNow") or "0"))
        for tax_free in sorted(grouped):
            bucket = grouped[tax_free]
            cumulative += bucket["quantity"]
            milestones.append(
                {
                    "instrumentKey": holding.get("instrumentKey", ""),
                    "ticker": holding.get("ticker", ""),
                    "isin": holding.get("isin", ""),
                    "name": holding.get("name", ""),
                    "taxFreeDate": tax_free,
                    "quantity": _qty(bucket["quantity"]),
                    "cumulativeTaxFreeQuantity": _qty(cumulative),
                    "costCzk": _num(bucket["cost"]),
                }
            )

    milestones.sort(key=lambda item: (item["taxFreeDate"], item["ticker"], item["instrumentKey"]))
    return milestones
