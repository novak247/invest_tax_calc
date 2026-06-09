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


def plan_batch(
    transactions: list[Trade],
    *,
    rates: dict[str, Decimal],
    rows: list[dict[str, Any]],
    sale_date: str,
) -> dict[str, Any]:
    if not rows:
        raise ValueError("Add at least one planned sale row.")

    planned_date = _parse_date(sale_date or date.today().isoformat())
    baseline = analyze_transactions(
        transactions,
        rates=rates,
        tax_year=planned_date.year,
        as_of=planned_date,
    )
    if not baseline:
        raise ValueError("Analyze at least one transaction before planning sales.")

    holdings_by_key = {
        str(holding["instrumentKey"]): holding
        for holding in baseline.get("holdings", [])
    }
    planned_trades: list[Trade] = []
    planned_inputs: list[dict[str, Any]] = []
    quantities_by_key: dict[str, Decimal] = defaultdict(Decimal)

    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError("Planned sale rows must be objects.")
        instrument_key = str(row.get("instrumentKey") or "")
        if not instrument_key:
            raise ValueError(f"Choose an instrument for planned sale row {index}.")
        holding = holdings_by_key.get(instrument_key)
        if holding is None:
            raise ValueError(f"No open holding was found for {instrument_key}.")

        quantity = _parse_decimal(str(row.get("quantity") or ""), f"quantity in row {index}")
        price = _parse_decimal(
            str(row.get("pricePerShareCzk") or ""),
            f"CZK price per share in row {index}",
        )
        if quantity <= 0:
            raise ValueError(f"Quantity in row {index} must be greater than zero.")
        if price <= 0:
            raise ValueError(f"CZK price per share in row {index} must be greater than zero.")

        quantities_by_key[instrument_key] += quantity
        available = Decimal(str(holding.get("quantity") or "0"))
        if quantities_by_key[instrument_key] > available + Decimal("0.00000001"):
            raise ValueError(
                f"Planned quantity for {holding.get('ticker') or instrument_key} exceeds "
                f"the open holding of {_qty(available)}."
            )

        source_id = f"{PLANNED_SALE_ID}:{index:04d}"
        planned_trades.append(
            Trade(
                source="Planner",
                source_id=source_id,
                action="Planned sale",
                kind="sell",
                traded_at=datetime.combine(planned_date, time(hour=12)),
                instrument_key=instrument_key,
                ticker=str(holding.get("ticker") or ""),
                isin=str(holding.get("isin") or ""),
                name=str(holding.get("name") or ""),
                quantity=quantity,
                gross=Money(quantity * price, "CZK"),
                fees=(),
            )
        )
        planned_inputs.append(
            {
                "sourceId": source_id,
                "instrumentKey": instrument_key,
                "ticker": str(holding.get("ticker") or ""),
                "isin": str(holding.get("isin") or ""),
                "name": str(holding.get("name") or ""),
                "quantity": quantity,
                "pricePerShareCzk": price,
                "priceSource": str(row.get("priceSource") or "manual"),
                "reasonSelected": str(row.get("reasonSelected") or ""),
            }
        )

    after = analyze_transactions(
        transactions + planned_trades,
        rates=rates,
        tax_year=planned_date.year,
        as_of=planned_date,
    )
    planned_source_ids = {row["sourceId"] for row in planned_inputs}
    planned_matches = [
        match
        for match in after.get("matches", [])
        if match.get("saleSourceId") in planned_source_ids
    ]
    after_holdings = {
        str(holding["instrumentKey"]): holding
        for holding in after.get("holdings", [])
    }
    result_rows = [
        _planned_row_payload(
            row,
            [
                match
                for match in planned_matches
                if match.get("saleSourceId") == row["sourceId"]
            ],
            after_holdings.get(row["instrumentKey"]),
        )
        for row in planned_inputs
    ]

    baseline_summary = baseline["summary"]
    after_summary = after["summary"]
    estimated_proceeds = sum(
        (row["quantity"] * row["pricePerShareCzk"] for row in planned_inputs),
        Decimal("0"),
    )
    taxable_gain_delta = (
        Decimal(str(after_summary["taxableGainCzk"]))
        - Decimal(str(baseline_summary["taxableGainCzk"]))
    )
    tax_delta = (
        Decimal(str(after_summary["estimatedTax15Czk"]))
        - Decimal(str(baseline_summary["estimatedTax15Czk"]))
    )
    warnings = list(after.get("warnings", []))
    if baseline_summary["grossLimitApplies"] and not after_summary["grossLimitApplies"]:
        warnings.append(
            "This plan pushes annual gross proceeds above 100,000 CZK, so earlier "
            "short-term sales in the same year may become taxable too."
        )

    return {
        "saleDate": planned_date.isoformat(),
        "targetProceedsCzk": None,
        "estimatedProceedsCzk": _num(estimated_proceeds),
        "shortfallCzk": 0.0,
        "overageCzk": 0.0,
        "taxableGainDeltaCzk": _num(taxable_gain_delta),
        "estimatedTaxDelta15Czk": _num(tax_delta),
        "deltaTaxableGainCzk": _num(taxable_gain_delta),
        "deltaEstimatedTax15Czk": _num(tax_delta),
        "totalGrossProceedsAfterPlanCzk": after_summary["grossProceedsCzk"],
        "rows": result_rows,
        "lotGroups": _planned_match_groups(planned_matches),
        "plannedMatches": planned_matches,
        "baselineSummary": baseline_summary,
        "afterSummary": after_summary,
        "warnings": warnings,
    }


def plan_target_proceeds(
    transactions: list[Trade],
    *,
    rates: dict[str, Decimal],
    target_proceeds_czk: str,
    sale_date: str,
    optimization_mode: str,
    candidate_instrument_keys: list[str] | None,
    quotes: dict[str, Any],
) -> dict[str, Any]:
    target = _parse_decimal(target_proceeds_czk, "target proceeds")
    if target <= 0:
        raise ValueError("Target proceeds must be greater than zero.")

    planned_date = _parse_date(sale_date or date.today().isoformat())
    baseline = analyze_transactions(
        transactions,
        rates=rates,
        tax_year=planned_date.year,
        as_of=planned_date,
    )
    if not baseline:
        raise ValueError("Analyze at least one transaction before optimizing sales.")

    valid_modes = {"min_tax", "min_gain", "preserve_tax_free", "fifo"}
    requested_mode = optimization_mode if optimization_mode in valid_modes else "min_tax"
    candidate_keys = {
        str(key)
        for key in (candidate_instrument_keys or [])
        if str(key)
    }
    holdings = [
        holding
        for holding in baseline.get("holdings", [])
        if not candidate_keys or str(holding.get("instrumentKey")) in candidate_keys
    ]
    if not holdings:
        raise ValueError("Choose at least one open holding for the optimizer.")

    prices: dict[str, Decimal] = {}
    missing_prices: list[str] = []
    for holding in holdings:
        key = str(holding["instrumentKey"])
        quote = quotes.get(key, {}) if isinstance(quotes, dict) else {}
        raw_price = quote.get("pricePerShareCzk") if isinstance(quote, dict) else quote
        try:
            price = _parse_decimal(str(raw_price or ""), f"price for {key}")
        except ValueError:
            missing_prices.append(str(holding.get("ticker") or key))
            continue
        if price <= 0:
            missing_prices.append(str(holding.get("ticker") or key))
            continue
        prices[key] = price
    if missing_prices:
        raise ValueError(
            "Enter or fetch a CZK price for: " + ", ".join(sorted(missing_prices))
        )

    strategies = (
        ["min_tax", "min_gain", "fifo"]
        if requested_mode == "min_tax"
        else [requested_mode]
    )
    evaluated: list[tuple[str, dict[str, Any], int, int]] = []
    for strategy in strategies:
        generated_rows, candidate_count, selected_count = _target_rows(
            holdings,
            prices,
            target,
            strategy,
        )
        if not generated_rows:
            continue
        scenario = plan_batch(
            transactions,
            rates=rates,
            rows=generated_rows,
            sale_date=planned_date.isoformat(),
        )
        achieved = Decimal(str(scenario["estimatedProceedsCzk"]))
        scenario["targetProceedsCzk"] = _num(target)
        scenario["shortfallCzk"] = _num(max(target - achieved, Decimal("0")))
        scenario["overageCzk"] = _num(max(achieved - target, Decimal("0")))
        evaluated.append((strategy, scenario, candidate_count, selected_count))

    if not evaluated:
        raise ValueError("The selected holdings do not have any quantity available to sell.")

    strategy, result, candidate_count, selected_count = min(
        evaluated,
        key=lambda item: (
            Decimal(str(item[1]["shortfallCzk"])),
            Decimal(str(item[1]["estimatedTaxDelta15Czk"])),
            Decimal(str(item[1]["taxableGainDeltaCzk"])),
            Decimal(str(item[1]["overageCzk"])),
        ),
    )
    target_reached = Decimal(str(result["shortfallCzk"])) <= Decimal("0.01")
    if not target_reached:
        result["warnings"].append(
            "The selected holdings cannot fully reach the requested target proceeds."
        )
    result["optimizer"] = {
        "mode": requested_mode,
        "targetReached": target_reached,
        "candidateCount": candidate_count,
        "selectedLotCount": selected_count,
        "strategy": strategy,
        "evaluatedStrategies": [item[0] for item in evaluated],
    }
    return result


def _target_rows(
    holdings: list[dict[str, Any]],
    prices: dict[str, Decimal],
    target: Decimal,
    mode: str,
) -> tuple[list[dict[str, Any]], int, int]:
    queues = {
        str(holding["instrumentKey"]): list(holding.get("lots", []))
        for holding in holdings
    }
    holding_by_key = {str(holding["instrumentKey"]): holding for holding in holdings}
    indexes = {key: 0 for key in queues}
    selected: dict[str, Decimal] = defaultdict(Decimal)
    selected_lots: dict[str, list[dict[str, Any]]] = defaultdict(list)
    remaining = target
    candidate_count = sum(len(lots) for lots in queues.values())
    selected_count = 0

    while remaining > Decimal("0.00000001"):
        available: list[tuple[tuple[Any, ...], str, dict[str, Any]]] = []
        for key, lots in queues.items():
            index = indexes[key]
            if index >= len(lots):
                continue
            lot = lots[index]
            available.append((_target_lot_score(lot, prices[key], mode, key), key, lot))
        if not available:
            break

        _, key, lot = min(available, key=lambda item: item[0])
        lot_quantity = Decimal(str(lot.get("quantity") or "0"))
        price = prices[key]
        quantity = min(lot_quantity, remaining / price)
        if quantity <= 0:
            indexes[key] += 1
            continue

        selected[key] += quantity
        selected_lots[key].append(lot)
        selected_count += 1
        remaining -= quantity * price
        if quantity >= lot_quantity - Decimal("0.00000001"):
            indexes[key] += 1
        else:
            break

    rows: list[dict[str, Any]] = []
    for key, quantity in selected.items():
        holding = holding_by_key[key]
        lots = selected_lots[key]
        exempt_count = sum(1 for lot in lots if lot.get("timeTestPassed"))
        reason = _optimizer_reason(mode, exempt_count, len(lots))
        rows.append(
            {
                "instrumentKey": key,
                "quantity": str(quantity),
                "pricePerShareCzk": str(prices[key]),
                "priceSource": "mixed",
                "reasonSelected": reason,
                "_ticker": str(holding.get("ticker") or ""),
            }
        )
    rows.sort(key=lambda row: (str(row.get("_ticker")), str(row["instrumentKey"])))
    return rows, candidate_count, selected_count


def _target_lot_score(
    lot: dict[str, Any],
    price: Decimal,
    mode: str,
    instrument_key: str,
) -> tuple[Any, ...]:
    quantity = Decimal(str(lot.get("quantity") or "0"))
    cost = Decimal(str(lot.get("costCzk") or "0"))
    proceeds = quantity * price
    gain_ratio = (proceeds - cost) / proceeds if proceeds else Decimal("0")
    time_test_passed = bool(lot.get("timeTestPassed"))
    acquired_at = str(lot.get("acquiredAt") or "9999-12-31")
    if mode == "fifo":
        return (acquired_at, instrument_key)
    if mode == "preserve_tax_free":
        return (time_test_passed, gain_ratio, acquired_at, instrument_key)
    if mode == "min_gain":
        return (gain_ratio, not time_test_passed, acquired_at, instrument_key)
    return (not time_test_passed, gain_ratio, acquired_at, instrument_key)


def _optimizer_reason(mode: str, exempt_count: int, lot_count: int) -> str:
    if mode == "preserve_tax_free":
        return "Preserves 3-year-exempt lots where FIFO permits."
    if mode == "fifo":
        return "Uses the earliest available FIFO lots."
    if mode == "min_gain":
        return "Uses the lowest estimated gain-per-proceeds FIFO path."
    if exempt_count == lot_count:
        return "Uses lots already past the 3-year time test."
    if exempt_count:
        return "Uses exempt lots first, then the lowest-gain FIFO path."
    return "Uses the lowest estimated tax-cost FIFO path."


def _planned_row_payload(
    row: dict[str, Any],
    matches: list[dict[str, Any]],
    remaining_holding: dict[str, Any] | None,
) -> dict[str, Any]:
    proceeds = row["quantity"] * row["pricePerShareCzk"]
    taxable_gain = max(
        sum(
            (
                Decimal(str(match.get("gainCzk") or "0"))
                for match in matches
                if match.get("taxable")
            ),
            Decimal("0"),
        ),
        Decimal("0"),
    )
    exempt_proceeds = sum(
        (
            Decimal(str(match.get("grossProceedsCzk") or "0"))
            for match in matches
            if not match.get("taxable")
        ),
        Decimal("0"),
    )
    statuses = list(dict.fromkeys(str(match.get("status") or "") for match in matches))
    return {
        "instrumentKey": row["instrumentKey"],
        "ticker": row["ticker"],
        "isin": row["isin"],
        "name": row["name"],
        "quantity": _qty(row["quantity"]),
        "pricePerShareCzk": _num(row["pricePerShareCzk"]),
        "priceSource": row["priceSource"],
        "estimatedProceedsCzk": _num(proceeds),
        "taxableGainCzk": _num(taxable_gain),
        "estimatedTax15Czk": _num(taxable_gain * TAX_RATE_BASIC),
        "exemptProceedsCzk": _num(exempt_proceeds),
        "remainingQuantity": (
            float(remaining_holding["quantity"]) if remaining_holding else 0.0
        ),
        "taxStatusSummary": "; ".join(statuses) or "No matched lots",
        "reasonSelected": row["reasonSelected"],
    }


def _planned_match_groups(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        matches,
        key=lambda match: (
            match.get("instrumentKey") or "",
            match.get("buyDate") or "9999-12-31",
            match.get("status") or "",
            match.get("buySourceId") or "",
        ),
    )
    groups: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for match in ordered:
        key = (
            str(match.get("instrumentKey") or ""),
            bool(match.get("taxable")),
            str(match.get("status") or ""),
        )
        if current is None or current["_key"] != key:
            current = {
                "_key": key,
                "instrumentKey": key[0],
                "ticker": str(match.get("ticker") or ""),
                "isin": str(match.get("isin") or ""),
                "name": str(match.get("name") or ""),
                "taxable": key[1],
                "status": key[2],
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
                "instrumentKey": group["instrumentKey"],
                "ticker": group["ticker"],
                "isin": group["isin"],
                "name": group["name"],
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
