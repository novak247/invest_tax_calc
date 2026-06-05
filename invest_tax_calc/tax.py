from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from .models import Money, TaxLot, Trade


GROSS_PROCEEDS_EXEMPTION_CZK = Decimal("100000")
TIME_TEST_YEARS = 3
TAX_RATE_BASIC = Decimal("0.15")
PLANNED_SALE_ID = "planned-sale"


def parse_rate_table(text: str) -> dict[str, Decimal]:
    rates: dict[str, Decimal] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
        elif ":" in line and line.count(":") == 1:
            key, value = line.split(":", 1)
        else:
            parts = line.replace(",", ".").split()
            if len(parts) != 2:
                continue
            key, value = parts

        key = key.strip().upper().replace(" ", "")
        try:
            rates[key] = Decimal(value.strip().replace(",", "."))
        except InvalidOperation:
            continue
    return rates


def analyze_transactions(
    transactions: list[Trade],
    *,
    rates: dict[str, Decimal],
    tax_year: int | None = None,
    as_of: str | date | None = None,
) -> dict[str, Any]:
    if not transactions:
        return {}

    as_of_date = _parse_date(as_of) if as_of else date.today()
    matches, holdings, warnings = _match_lots(transactions, rates)
    years = sorted({match["sale_date"].year for match in matches})
    selected_year = tax_year or (years[-1] if years else as_of_date.year)

    summaries = {
        year: _summarize_year(matches, year)
        for year in sorted(set(years) | {selected_year})
    }
    selected_summary = summaries[selected_year]
    holdings_payload = _holdings_payload(holdings, as_of_date)

    return {
        "selectedYear": selected_year,
        "asOf": as_of_date.isoformat(),
        "transactionCount": len(transactions),
        "buyCount": sum(1 for tx in transactions if tx.kind == "buy"),
        "sellCount": sum(1 for tx in transactions if tx.kind == "sell"),
        "years": sorted(summaries),
        "warnings": warnings,
        "summary": selected_summary,
        "annualSummaries": {str(year): summary for year, summary in summaries.items()},
        "matches": [
            _match_payload(match, selected_summary["grossLimitApplies"])
            for match in matches
            if match["sale_date"].year == selected_year
        ],
        "holdings": holdings_payload,
    }


def plan_sale(
    transactions: list[Trade],
    *,
    rates: dict[str, Decimal],
    instrument_key: str,
    quantity: str,
    price_per_share_czk: str,
    sale_date: str,
) -> dict[str, Any]:
    if not instrument_key:
        raise ValueError("Choose an instrument to plan.")

    qty = _parse_decimal(quantity, "planned quantity")
    if qty <= 0:
        raise ValueError("Planned quantity must be greater than zero.")

    price = _parse_decimal(price_per_share_czk, "planned CZK price per share")
    if price <= 0:
        raise ValueError("Planned CZK price per share must be greater than zero.")

    planned_date = _parse_date(sale_date or date.today().isoformat())
    baseline = analyze_transactions(
        transactions,
        rates=rates,
        tax_year=planned_date.year,
        as_of=planned_date,
    )

    display = _find_instrument_display(baseline.get("holdings", []), instrument_key)
    planned = Trade(
        source="Planner",
        source_id=PLANNED_SALE_ID,
        action="Planned sale",
        kind="sell",
        traded_at=datetime.combine(planned_date, time(hour=12)),
        instrument_key=instrument_key,
        ticker=display.get("ticker", ""),
        isin=display.get("isin", ""),
        name=display.get("name", ""),
        quantity=qty,
        gross=Money(qty * price, "CZK"),
        fees=(),
    )

    after = analyze_transactions(
        transactions + [planned],
        rates=rates,
        tax_year=planned_date.year,
        as_of=planned_date,
    )
    planned_matches = [
        match
        for match in after["matches"]
        if match["saleSourceId"] == PLANNED_SALE_ID
    ]
    planned_match_groups = _planned_match_groups(planned_matches)

    return {
        "saleDate": planned_date.isoformat(),
        "instrumentKey": instrument_key,
        "plannedProceedsCzk": _num(qty * price),
        "baselineSummary": baseline["summary"],
        "afterSummary": after["summary"],
        "deltaTaxableGainCzk": _num(
            Decimal(str(after["summary"]["taxableGainCzk"]))
            - Decimal(str(baseline["summary"]["taxableGainCzk"]))
        ),
        "deltaEstimatedTax15Czk": _num(
            Decimal(str(after["summary"]["estimatedTax15Czk"]))
            - Decimal(str(baseline["summary"]["estimatedTax15Czk"]))
        ),
        "plannedMatches": planned_matches,
        "plannedMatchGroups": planned_match_groups,
        "warnings": after.get("warnings", []),
    }


def _planned_match_groups(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        matches,
        key=lambda match: (
            match.get("buyDate") or "9999-12-31",
            match.get("status") or "",
            match.get("buySourceId") or "",
        ),
    )
    groups: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for match in ordered:
        key = (bool(match.get("taxable")), str(match.get("status") or ""))
        if current is None or current["_key"] != key:
            current = {
                "_key": key,
                "taxable": key[0],
                "status": key[1],
                "saleDate": match.get("saleDate"),
                "buyDateStart": match.get("buyDate"),
                "buyDateEnd": match.get("buyDate"),
                "lotCount": 0,
                "quantity": Decimal("0"),
                "grossProceedsCzk": Decimal("0"),
                "costCzk": Decimal("0"),
                "gainCzk": Decimal("0"),
                "saleFeesCzk": Decimal("0"),
            }
            groups.append(current)

        buy_date = match.get("buyDate")
        if buy_date:
            if not current["buyDateStart"] or buy_date < current["buyDateStart"]:
                current["buyDateStart"] = buy_date
            if not current["buyDateEnd"] or buy_date > current["buyDateEnd"]:
                current["buyDateEnd"] = buy_date

        current["lotCount"] += 1
        current["quantity"] += Decimal(str(match.get("quantity") or "0"))
        current["grossProceedsCzk"] += Decimal(str(match.get("grossProceedsCzk") or "0"))
        current["costCzk"] += Decimal(str(match.get("costCzk") or "0"))
        current["gainCzk"] += Decimal(str(match.get("gainCzk") or "0"))
        current["saleFeesCzk"] += Decimal(str(match.get("saleFeesCzk") or "0"))

    payload: list[dict[str, Any]] = []
    for group in groups:
        payload.append(
            {
                "taxable": bool(group["taxable"]),
                "status": str(group["status"]),
                "saleDate": group["saleDate"],
                "buyDateStart": group["buyDateStart"],
                "buyDateEnd": group["buyDateEnd"],
                "lotCount": int(group["lotCount"]),
                "quantity": _qty(group["quantity"]),
                "grossProceedsCzk": _num(group["grossProceedsCzk"]),
                "costCzk": _num(group["costCzk"]),
                "gainCzk": _num(group["gainCzk"]),
                "saleFeesCzk": _num(group["saleFeesCzk"]),
            }
        )
    return payload


def _match_lots(
    transactions: list[Trade],
    rates: dict[str, Decimal],
) -> tuple[list[dict[str, Any]], dict[str, list[TaxLot]], list[str]]:
    lots: dict[str, list[TaxLot]] = defaultdict(list)
    matches: list[dict[str, Any]] = []
    warnings: list[str] = []

    ordered = sorted(
        transactions,
        key=lambda tx: (tx.traded_at, 0 if tx.kind == "buy" else 1, tx.source_id),
    )
    for tx in ordered:
        gross_czk = _convert(tx.gross, tx.traded_at.date(), rates)
        fees_czk = sum((_convert(fee, tx.traded_at.date(), rates) for fee in tx.fees), Decimal("0"))

        if tx.kind == "buy":
            lots[tx.instrument_key].append(
                TaxLot(
                    source_id=tx.source_id,
                    instrument_key=tx.instrument_key,
                    ticker=tx.ticker,
                    isin=tx.isin,
                    name=tx.name,
                    acquired_at=tx.traded_at,
                    quantity_total=tx.quantity,
                    quantity_remaining=tx.quantity,
                    cost_czk_total=gross_czk + fees_czk,
                )
            )
            continue

        if tx.kind != "sell":
            continue

        remaining = tx.quantity
        source_lots = lots[tx.instrument_key]
        for lot in source_lots:
            if remaining <= 0:
                break
            if lot.quantity_remaining <= 0:
                continue

            matched_qty = min(remaining, lot.quantity_remaining)
            sale_ratio = matched_qty / tx.quantity
            proceeds = gross_czk * sale_ratio
            sale_fees = fees_czk * sale_ratio
            cost = lot.cost_per_unit_czk * matched_qty
            tax_free_date = _add_years(lot.acquired_at.date(), TIME_TEST_YEARS)

            matches.append(
                {
                    "sale_source_id": tx.source_id,
                    "buy_source_id": lot.source_id,
                    "instrument_key": tx.instrument_key,
                    "ticker": tx.ticker or lot.ticker,
                    "isin": tx.isin or lot.isin,
                    "name": tx.name or lot.name,
                    "sale_date": tx.traded_at.date(),
                    "buy_date": lot.acquired_at.date(),
                    "quantity": matched_qty,
                    "gross_proceeds_czk": proceeds,
                    "sale_fees_czk": sale_fees,
                    "cost_czk": cost,
                    "gain_czk": proceeds - sale_fees - cost,
                    "time_test_passed": tx.traded_at.date() >= tax_free_date,
                    "tax_free_date": tax_free_date,
                    "missing_cost": False,
                }
            )
            lot.quantity_remaining -= matched_qty
            remaining -= matched_qty

        if remaining > Decimal("0.00000001"):
            ratio = remaining / tx.quantity
            proceeds = gross_czk * ratio
            sale_fees = fees_czk * ratio
            warnings.append(
                f"Sell order {tx.source_id} for {tx.ticker or tx.instrument_key} has "
                f"{remaining} units without a matching buy lot. Cost was treated as 0 CZK."
            )
            matches.append(
                {
                    "sale_source_id": tx.source_id,
                    "buy_source_id": "",
                    "instrument_key": tx.instrument_key,
                    "ticker": tx.ticker,
                    "isin": tx.isin,
                    "name": tx.name,
                    "sale_date": tx.traded_at.date(),
                    "buy_date": None,
                    "quantity": remaining,
                    "gross_proceeds_czk": proceeds,
                    "sale_fees_czk": sale_fees,
                    "cost_czk": Decimal("0"),
                    "gain_czk": proceeds - sale_fees,
                    "time_test_passed": False,
                    "tax_free_date": None,
                    "missing_cost": True,
                }
            )

    return matches, lots, warnings


def _summarize_year(matches: list[dict[str, Any]], year: int) -> dict[str, Any]:
    yearly = [match for match in matches if match["sale_date"].year == year]
    gross = sum((match["gross_proceeds_czk"] for match in yearly), Decimal("0"))
    gross_limit_applies = gross <= GROSS_PROCEEDS_EXEMPTION_CZK

    taxable_proceeds = Decimal("0")
    taxable_cost = Decimal("0")
    taxable_fees = Decimal("0")
    exempt_proceeds = Decimal("0")
    time_exempt_proceeds = Decimal("0")
    gross_limit_exempt_proceeds = Decimal("0")

    for match in yearly:
        if gross_limit_applies:
            exempt_proceeds += match["gross_proceeds_czk"]
            gross_limit_exempt_proceeds += match["gross_proceeds_czk"]
        elif match["time_test_passed"]:
            exempt_proceeds += match["gross_proceeds_czk"]
            time_exempt_proceeds += match["gross_proceeds_czk"]
        else:
            taxable_proceeds += match["gross_proceeds_czk"]
            taxable_cost += match["cost_czk"]
            taxable_fees += match["sale_fees_czk"]

    taxable_result = taxable_proceeds - taxable_cost - taxable_fees
    taxable_gain = max(taxable_result, Decimal("0"))

    return {
        "year": year,
        "grossProceedsCzk": _num(gross),
        "grossLimitCzk": _num(GROSS_PROCEEDS_EXEMPTION_CZK),
        "grossLimitApplies": gross_limit_applies,
        "taxableProceedsCzk": _num(taxable_proceeds),
        "taxableCostCzk": _num(taxable_cost),
        "taxableFeesCzk": _num(taxable_fees),
        "taxableResultCzk": _num(taxable_result),
        "taxableGainCzk": _num(taxable_gain),
        "estimatedTax15Czk": _num(taxable_gain * TAX_RATE_BASIC),
        "exemptProceedsCzk": _num(exempt_proceeds),
        "timeExemptProceedsCzk": _num(time_exempt_proceeds),
        "grossLimitExemptProceedsCzk": _num(gross_limit_exempt_proceeds),
        "sellMatchCount": len(yearly),
    }


def _holdings_payload(holdings: dict[str, list[TaxLot]], as_of: date) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for instrument_key, lots in sorted(holdings.items()):
        open_lots = [lot for lot in lots if lot.quantity_remaining > Decimal("0.00000001")]
        if not open_lots:
            continue

        total_qty = sum((lot.quantity_remaining for lot in open_lots), Decimal("0"))
        total_cost = sum(
            (lot.cost_per_unit_czk * lot.quantity_remaining for lot in open_lots),
            Decimal("0"),
        )
        matured_qty = sum(
            (
                lot.quantity_remaining
                for lot in open_lots
                if as_of >= _add_years(lot.acquired_at.date(), TIME_TEST_YEARS)
            ),
            Decimal("0"),
        )
        future_dates = [
            _add_years(lot.acquired_at.date(), TIME_TEST_YEARS)
            for lot in open_lots
            if as_of < _add_years(lot.acquired_at.date(), TIME_TEST_YEARS)
        ]
        first = open_lots[0]
        payload.append(
            {
                "instrumentKey": instrument_key,
                "ticker": first.ticker,
                "isin": first.isin,
                "name": first.name,
                "quantity": _qty(total_qty),
                "costCzk": _num(total_cost),
                "averageCostCzk": _num(total_cost / total_qty if total_qty else Decimal("0")),
                "taxFreeQuantityNow": _qty(matured_qty),
                "nextTaxFreeDate": min(future_dates).isoformat() if future_dates else None,
                "lots": [
                    {
                        "sourceId": lot.source_id,
                        "acquiredAt": lot.acquired_at.date().isoformat(),
                        "quantity": _qty(lot.quantity_remaining),
                        "costCzk": _num(lot.cost_per_unit_czk * lot.quantity_remaining),
                        "averageCostCzk": _num(lot.cost_per_unit_czk),
                        "taxFreeDate": _add_years(lot.acquired_at.date(), TIME_TEST_YEARS).isoformat(),
                        "timeTestPassed": as_of
                        >= _add_years(lot.acquired_at.date(), TIME_TEST_YEARS),
                    }
                    for lot in open_lots
                ],
            }
        )
    return payload


def _match_payload(match: dict[str, Any], gross_limit_applies: bool) -> dict[str, Any]:
    if gross_limit_applies:
        status = "Exempt: annual gross proceeds <= 100,000 CZK"
        taxable = False
    elif match["time_test_passed"]:
        status = "Exempt: 3-year time test"
        taxable = False
    elif match["missing_cost"]:
        status = "Taxable: missing buy lot, cost treated as 0 CZK"
        taxable = True
    else:
        status = "Taxable: time test not met"
        taxable = True

    return {
        "saleSourceId": match["sale_source_id"],
        "buySourceId": match["buy_source_id"],
        "instrumentKey": match["instrument_key"],
        "ticker": match["ticker"],
        "isin": match["isin"],
        "name": match["name"],
        "saleDate": match["sale_date"].isoformat(),
        "buyDate": match["buy_date"].isoformat() if match["buy_date"] else None,
        "quantity": _qty(match["quantity"]),
        "grossProceedsCzk": _num(match["gross_proceeds_czk"]),
        "saleFeesCzk": _num(match["sale_fees_czk"]),
        "costCzk": _num(match["cost_czk"]),
        "gainCzk": _num(match["gain_czk"]),
        "taxFreeDate": match["tax_free_date"].isoformat() if match["tax_free_date"] else None,
        "timeTestPassed": bool(match["time_test_passed"]),
        "taxable": taxable,
        "status": status,
    }


def _find_instrument_display(holdings: list[dict[str, Any]], instrument_key: str) -> dict[str, str]:
    for holding in holdings:
        if holding.get("instrumentKey") == instrument_key:
            return {
                "ticker": str(holding.get("ticker") or ""),
                "isin": str(holding.get("isin") or ""),
                "name": str(holding.get("name") or ""),
            }
    return {}


def _convert(money: Money, when: date, rates: dict[str, Decimal]) -> Decimal:
    currency = money.currency.strip().upper()
    amount = abs(money.amount)
    if currency == "CZK":
        return amount

    year_key = f"{currency}:{when.year}"
    rate = rates.get(year_key) or rates.get(currency)
    if rate is None:
        raise ValueError(
            f"Missing CZK FX rate for {currency} in {when.year}. "
            f"Add {currency}:{when.year}=... or {currency}=... in the FX rates box."
        )
    return amount * rate


def _parse_decimal(value: str, label: str) -> Decimal:
    cleaned = str(value).strip().replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid {label}: {value}") from exc


def _parse_date(value: str | date | None) -> date:
    if isinstance(value, date):
        return value
    if not value:
        return date.today()
    return date.fromisoformat(str(value)[:10])


def _add_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(month=2, day=28, year=value.year + years)


def _num(value: Decimal) -> float:
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def _qty(value: Decimal) -> float:
    rounded = value.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    return float(rounded)
