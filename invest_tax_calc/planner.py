from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, ROUND_UP
from typing import Any, Callable

from .models import Money, Trade
from .tax import (
    PLANNED_SALE_ID,
    QTY_EPSILON,
    TAX_RATE_BASIC,
    TIME_TEST_YEARS,
    _add_years,
    _ensure_sellable_quantity,
    _find_instrument_display,
    _match_lots,
    _num,
    _parse_date,
    _parse_decimal,
    _qty,
    _transactions_on_or_before,
    analyze_transactions,
)

OPTIMIZATION_MODES = ("min_tax", "min_taxable_gain", "preserve_tax_free", "fifo")
PROCEEDS_TOLERANCE_CZK = Decimal("0.01")


def plan_sale_batch(
    transactions: list[Trade],
    *,
    rates: dict[str, Decimal],
    sale_date: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Evaluate several planned sells as one scenario.

    The Czech 100,000 CZK annual gross proceeds exemption is global per year,
    so the rows must be analyzed together, never one by one.
    """
    if not transactions:
        raise ValueError("Load and analyze statements before planning.")
    if not rows:
        raise ValueError("Add at least one sale row to plan.")

    planned_date = _parse_date(sale_date or date.today().isoformat())
    baseline = analyze_transactions(
        transactions,
        rates=rates,
        tax_year=planned_date.year,
        as_of=planned_date,
    )
    holdings = baseline.get("holdings", [])

    planned_trades: list[Trade] = []
    row_inputs: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        instrument_key = str(row.get("instrumentKey") or "").strip()
        if not instrument_key:
            raise ValueError(f"Row {index}: choose an instrument.")
        qty = _parse_decimal(str(row.get("quantity") or ""), f"row {index} quantity")
        if qty <= 0:
            raise ValueError(f"Row {index}: quantity must be greater than zero.")
        price = _parse_decimal(
            str(row.get("pricePerShareCzk") or ""), f"row {index} CZK price per share"
        )
        if price <= 0:
            raise ValueError(f"Row {index}: CZK price per share must be greater than zero.")

        source_id = f"{PLANNED_SALE_ID}:{index}"
        display = _find_instrument_display(holdings, instrument_key)
        planned_trades.append(
            _planned_trade(
                source_id=source_id,
                planned_date=planned_date,
                instrument_key=instrument_key,
                display=display,
                quantity=qty,
                price=price,
            )
        )
        row_inputs.append(
            {
                "sourceId": source_id,
                "instrumentKey": instrument_key,
                "ticker": display.get("ticker", ""),
                "quantity": qty,
                "price": price,
            }
        )

    # Rows for the same instrument draw from the same holdings, so the
    # oversell check must validate their combined quantity, not each row.
    combined: dict[str, Decimal] = {}
    for row in row_inputs:
        combined[row["instrumentKey"]] = (
            combined.get(row["instrumentKey"], Decimal("0")) + row["quantity"]
        )
    for instrument_key, total_qty in combined.items():
        _ensure_sellable_quantity(holdings, instrument_key, total_qty, planned_date)

    after = analyze_transactions(
        transactions + planned_trades,
        rates=rates,
        tax_year=planned_date.year,
        as_of=planned_date,
    )
    planned_matches = _planned_matches(after)
    matches_by_source: dict[str, list[dict[str, Any]]] = {}
    for match in planned_matches:
        matches_by_source.setdefault(str(match["saleSourceId"]), []).append(match)

    rows_payload = []
    for row in row_inputs:
        row_matches = matches_by_source.get(row["sourceId"], [])
        rows_payload.append(
            {
                "instrumentKey": row["instrumentKey"],
                "ticker": row["ticker"],
                "quantity": _qty(row["quantity"]),
                "pricePerShareCzk": _num(row["price"]),
                "estimatedProceedsCzk": _num(row["quantity"] * row["price"]),
                "taxableGainCzk": _num(_taxable_gain(row_matches)),
                "taxStatusSummary": _tax_status_summary(row_matches),
                "remainingQuantity": _remaining_quantity(
                    holdings, row["instrumentKey"], row_inputs
                ),
            }
        )

    estimated_proceeds = sum((row["quantity"] * row["price"] for row in row_inputs), Decimal("0"))
    return {
        "saleDate": planned_date.isoformat(),
        "estimatedProceedsCzk": _num(estimated_proceeds),
        "baselineSummary": baseline["summary"],
        "afterSummary": after["summary"],
        "taxableGainDeltaCzk": _summary_delta(baseline, after, "taxableGainCzk"),
        "estimatedTaxDelta15Czk": _summary_delta(baseline, after, "estimatedTax15Czk"),
        "rows": rows_payload,
        "lotGroups": _planned_lot_groups(planned_matches),
        "warnings": after.get("warnings", []),
    }


def plan_target_proceeds(
    transactions: list[Trade],
    *,
    rates: dict[str, Decimal],
    sale_date: str,
    target_proceeds_czk: str,
    optimization_mode: str = "min_tax",
    candidate_instrument_keys: list[str] | None = None,
    quotes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pick what to sell to raise a fixed CZK amount with the least tax impact.

    Lots within one instrument are always consumed FIFO (matching the tax
    engine and broker reality); the optimizer chooses across instruments and
    how much of each to sell. Every candidate plan is scored by running the
    full-year analysis, so the global 100k gross proceeds rule is respected.
    """
    if not transactions:
        raise ValueError("Load and analyze statements before planning.")

    mode = (optimization_mode or "min_tax").strip()
    if mode not in OPTIMIZATION_MODES:
        raise ValueError(
            f"Unknown optimization mode: {mode}. Use one of {', '.join(OPTIMIZATION_MODES)}."
        )

    target = _parse_decimal(str(target_proceeds_czk), "target proceeds")
    if target <= 0:
        raise ValueError("Target proceeds must be greater than zero.")

    planned_date = _parse_date(sale_date or date.today().isoformat())
    baseline = analyze_transactions(
        transactions,
        rates=rates,
        tax_year=planned_date.year,
        as_of=planned_date,
    )
    holdings = baseline.get("holdings", [])

    warnings: list[str] = []
    prices = _parse_quotes(quotes or {}, warnings)
    lots_by_instrument = _candidate_lots(
        transactions,
        rates=rates,
        planned_date=planned_date,
        candidate_keys=candidate_instrument_keys,
        prices=prices,
        warnings=warnings,
    )
    if not lots_by_instrument:
        raise ValueError(
            "No sellable lots with prices. Refresh prices or enter them manually."
        )
    candidate_count = sum(len(lots) for lots in lots_by_instrument.values())

    best: dict[str, Any] | None = None
    best_score: tuple[Decimal, Decimal, Decimal] | None = None
    for strategy_name, score_fn in _strategies_for_mode(mode):
        selection, shortfall = _greedy_select(lots_by_instrument, target, score_fn)
        if not selection:
            continue
        scenario = _evaluate_scenario(
            transactions,
            baseline=baseline,
            holdings=holdings,
            selection=selection,
            prices=prices,
            planned_date=planned_date,
            rates=rates,
        )
        score = (
            Decimal(str(scenario["estimatedTaxDelta15Czk"])),
            Decimal(str(scenario["taxableGainDeltaCzk"])),
            shortfall,
        )
        if best_score is None or score < best_score:
            best_score = score
            best = scenario
            best["optimizerStrategy"] = strategy_name
            best["shortfall"] = shortfall

    if best is None:
        raise ValueError("The optimizer could not build a plan from the available lots.")

    shortfall = best.pop("shortfall")
    target_reached = shortfall <= PROCEEDS_TOLERANCE_CZK
    if not target_reached:
        warnings.append(
            f"Only {_num(target - shortfall)} CZK of the {_num(target)} CZK target is "
            "reachable with the selected instruments and prices."
        )

    best["targetProceedsCzk"] = _num(target)
    best["shortfallCzk"] = _num(shortfall)
    best["warnings"] = warnings + best.get("warnings", [])
    best["optimizer"] = {
        "mode": mode,
        "targetReached": target_reached,
        "candidateCount": candidate_count,
        "strategy": best.pop("optimizerStrategy"),
    }
    return best


@dataclass(frozen=True)
class _LotCandidate:
    instrument_key: str
    ticker: str
    isin: str
    name: str
    acquired_at: datetime
    quantity: Decimal
    cost_per_unit_czk: Decimal
    price_czk: Decimal
    exempt: bool

    @property
    def gain_per_czk(self) -> Decimal:
        return (self.price_czk - self.cost_per_unit_czk) / self.price_czk

    @property
    def tax_per_czk(self) -> Decimal:
        if self.exempt:
            return Decimal("0")
        return max(self.gain_per_czk, Decimal("0")) * TAX_RATE_BASIC


def _candidate_lots(
    transactions: list[Trade],
    *,
    rates: dict[str, Decimal],
    planned_date: date,
    candidate_keys: list[str] | None,
    prices: dict[str, Decimal],
    warnings: list[str],
) -> dict[str, list[_LotCandidate]]:
    # Lots bought after the planned sale date cannot be sold on it.
    _, holdings_lots, _ = _match_lots(
        _transactions_on_or_before(transactions, planned_date), rates
    )
    requested = [key for key in (candidate_keys or []) if key]
    keys = requested or sorted(holdings_lots)

    lots_by_instrument: dict[str, list[_LotCandidate]] = {}
    for key in keys:
        open_lots = [
            lot
            for lot in holdings_lots.get(key, [])
            if lot.quantity_remaining > QTY_EPSILON
        ]
        if not open_lots:
            if requested:
                warnings.append(f"No open lots for {key}; it was skipped.")
            continue

        price = prices.get(key)
        label = open_lots[0].ticker or key
        if price is None:
            warnings.append(
                f"No CZK price for {label}; it was excluded from the optimization."
            )
            continue

        lots_by_instrument[key] = [
            _LotCandidate(
                instrument_key=key,
                ticker=lot.ticker,
                isin=lot.isin,
                name=lot.name,
                acquired_at=lot.acquired_at,
                quantity=lot.quantity_remaining,
                cost_per_unit_czk=lot.cost_per_unit_czk,
                price_czk=price,
                exempt=planned_date
                >= _add_years(lot.acquired_at.date(), TIME_TEST_YEARS),
            )
            for lot in sorted(open_lots, key=lambda lot: lot.acquired_at)
        ]
    return lots_by_instrument


def _parse_quotes(quotes: dict[str, Any], warnings: list[str]) -> dict[str, Decimal]:
    prices: dict[str, Decimal] = {}
    for key, quote in quotes.items():
        raw = ""
        if isinstance(quote, dict):
            raw = str(quote.get("pricePerShareCzk") or quote.get("priceCzk") or "")
        else:
            raw = str(quote or "")
        if not raw.strip():
            continue
        try:
            price = _parse_decimal(raw, f"price for {key}")
        except ValueError:
            warnings.append(f"Ignored invalid price for {key}: {raw}")
            continue
        if price <= 0:
            warnings.append(f"Ignored non-positive price for {key}.")
            continue
        prices[key] = price
    return prices


_ScoreFn = Callable[[_LotCandidate], tuple]


def _strategies_for_mode(mode: str) -> list[tuple[str, _ScoreFn]]:
    def exempt_then_low_tax(lot: _LotCandidate) -> tuple:
        return (0 if lot.exempt else 1, lot.tax_per_czk, lot.acquired_at)

    def exempt_then_low_gain(lot: _LotCandidate) -> tuple:
        return (0 if lot.exempt else 1, lot.gain_per_czk, lot.acquired_at)

    def taxable_low_gain_first(lot: _LotCandidate) -> tuple:
        return (1 if lot.exempt else 0, lot.gain_per_czk, lot.acquired_at)

    def fifo(lot: _LotCandidate) -> tuple:
        return (lot.acquired_at,)

    if mode == "min_tax":
        # Try alternate sort orders and keep the best full-scenario delta;
        # the greedy ranking alone cannot see the global 100k interaction.
        return [
            ("exempt lots first, then lowest tax per CZK proceeds", exempt_then_low_tax),
            ("lowest taxable gain per CZK proceeds", exempt_then_low_gain),
            ("FIFO across all instruments", fifo),
        ]
    if mode == "min_taxable_gain":
        return [
            ("exempt lots first, then lowest gain per CZK proceeds", exempt_then_low_gain),
            ("FIFO across all instruments", fifo),
        ]
    if mode == "preserve_tax_free":
        return [("taxable lots first, lowest gain per CZK proceeds", taxable_low_gain_first)]
    return [("FIFO across all instruments", fifo)]


def _greedy_select(
    lots_by_instrument: dict[str, list[_LotCandidate]],
    target: Decimal,
    score_fn: _ScoreFn,
) -> tuple[dict[str, Decimal], Decimal]:
    """Greedily pick sell quantities until the target proceeds is reached.

    Only each instrument's earliest open lot is eligible at any moment (FIFO),
    so picking a "cheap" later lot always consumes the lots before it first.
    """
    cursors = {key: 0 for key in lots_by_instrument}
    selected: dict[str, Decimal] = {}
    remaining = target

    while remaining > PROCEEDS_TOLERANCE_CZK:
        frontier = [
            lots[cursors[key]]
            for key, lots in lots_by_instrument.items()
            if cursors[key] < len(lots)
        ]
        if not frontier:
            break
        lot = min(frontier, key=score_fn)
        lot_value = lot.quantity * lot.price_czk
        if lot_value <= remaining:
            qty = lot.quantity
            cursors[lot.instrument_key] += 1
        else:
            qty = (remaining / lot.price_czk).quantize(QTY_EPSILON, rounding=ROUND_UP)
            qty = min(qty, lot.quantity)
            cursors[lot.instrument_key] += 1
        selected[lot.instrument_key] = selected.get(lot.instrument_key, Decimal("0")) + qty
        remaining -= qty * lot.price_czk

    return selected, max(remaining, Decimal("0"))


def _evaluate_scenario(
    transactions: list[Trade],
    *,
    baseline: dict[str, Any],
    holdings: list[dict[str, Any]],
    selection: dict[str, Decimal],
    prices: dict[str, Decimal],
    planned_date: date,
    rates: dict[str, Decimal],
) -> dict[str, Any]:
    rows = [
        {"instrumentKey": key, "quantity": qty}
        for key, qty in selection.items()
        if qty > QTY_EPSILON
    ]
    planned_trades = []
    row_inputs = []
    for index, row in enumerate(rows, start=1):
        key = row["instrumentKey"]
        qty = row["quantity"]
        price = prices[key]
        display = _find_instrument_display(holdings, key)
        source_id = f"{PLANNED_SALE_ID}:{index}"
        planned_trades.append(
            _planned_trade(
                source_id=source_id,
                planned_date=planned_date,
                instrument_key=key,
                display=display,
                quantity=qty,
                price=price,
            )
        )
        row_inputs.append(
            {
                "sourceId": source_id,
                "instrumentKey": key,
                "ticker": display.get("ticker", ""),
                "quantity": qty,
                "price": price,
            }
        )

    after = analyze_transactions(
        transactions + planned_trades,
        rates=rates,
        tax_year=planned_date.year,
        as_of=planned_date,
    )
    planned_matches = _planned_matches(after)
    matches_by_source: dict[str, list[dict[str, Any]]] = {}
    for match in planned_matches:
        matches_by_source.setdefault(str(match["saleSourceId"]), []).append(match)

    rows_payload = []
    for row in row_inputs:
        row_matches = matches_by_source.get(row["sourceId"], [])
        rows_payload.append(
            {
                "instrumentKey": row["instrumentKey"],
                "ticker": row["ticker"],
                "quantity": _qty(row["quantity"]),
                "pricePerShareCzk": _num(row["price"]),
                "estimatedProceedsCzk": _num(row["quantity"] * row["price"]),
                "taxableGainCzk": _num(_taxable_gain(row_matches)),
                "taxStatusSummary": _tax_status_summary(row_matches),
                "remainingQuantity": _remaining_quantity(
                    holdings, row["instrumentKey"], row_inputs
                ),
            }
        )

    estimated = sum((row["quantity"] * row["price"] for row in row_inputs), Decimal("0"))
    return {
        "saleDate": planned_date.isoformat(),
        "estimatedProceedsCzk": _num(estimated),
        "baselineSummary": baseline["summary"],
        "afterSummary": after["summary"],
        "taxableGainDeltaCzk": _summary_delta(baseline, after, "taxableGainCzk"),
        "estimatedTaxDelta15Czk": _summary_delta(baseline, after, "estimatedTax15Czk"),
        "rows": rows_payload,
        "lotGroups": _planned_lot_groups(planned_matches),
        "warnings": after.get("warnings", []),
    }


def _planned_trade(
    *,
    source_id: str,
    planned_date: date,
    instrument_key: str,
    display: dict[str, str],
    quantity: Decimal,
    price: Decimal,
) -> Trade:
    return Trade(
        source="Planner",
        source_id=source_id,
        action="Planned sale",
        kind="sell",
        traded_at=datetime.combine(planned_date, time(hour=12)),
        instrument_key=instrument_key,
        ticker=display.get("ticker", ""),
        isin=display.get("isin", ""),
        name=display.get("name", ""),
        quantity=quantity,
        gross=Money(quantity * price, "CZK"),
        fees=(),
    )


def _planned_matches(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        match
        for match in analysis.get("matches", [])
        if str(match.get("saleSourceId") or "").startswith(PLANNED_SALE_ID)
    ]


def _taxable_gain(matches: list[dict[str, Any]]) -> Decimal:
    return sum(
        (
            Decimal(str(match.get("gainCzk") or "0"))
            for match in matches
            if match.get("taxable")
        ),
        Decimal("0"),
    )


def _tax_status_summary(matches: list[dict[str, Any]]) -> str:
    if not matches:
        return "No matching lots"

    total = Decimal("0")
    taxable_total = Decimal("0")
    exempt_statuses: dict[str, Decimal] = {}
    for match in matches:
        proceeds = Decimal(str(match.get("grossProceedsCzk") or "0"))
        total += proceeds
        if match.get("taxable"):
            taxable_total += proceeds
        else:
            status = str(match.get("status") or "Exempt")
            exempt_statuses[status] = exempt_statuses.get(status, Decimal("0")) + proceeds

    if total <= 0:
        return "No proceeds"
    if taxable_total <= 0:
        dominant = max(exempt_statuses, key=exempt_statuses.get)
        return dominant
    if taxable_total >= total:
        return "Taxable: time test not met"

    exempt_pct = int(((total - taxable_total) / total * 100).to_integral_value())
    if exempt_pct >= 50:
        return f"Mostly exempt ({exempt_pct}% of proceeds)"
    return f"Mostly taxable ({100 - exempt_pct}% of proceeds)"


def _remaining_quantity(
    holdings: list[dict[str, Any]],
    instrument_key: str,
    row_inputs: list[dict[str, Any]],
) -> float:
    held = Decimal("0")
    for holding in holdings:
        if holding.get("instrumentKey") == instrument_key:
            held = Decimal(str(holding.get("quantity") or "0"))
            break
    sold = sum(
        (row["quantity"] for row in row_inputs if row["instrumentKey"] == instrument_key),
        Decimal("0"),
    )
    return _qty(max(held - sold, Decimal("0")))


def _summary_delta(baseline: dict[str, Any], after: dict[str, Any], key: str) -> float:
    return _num(
        Decimal(str(after["summary"][key])) - Decimal(str(baseline["summary"][key]))
    )


def _planned_lot_groups(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate planned lot matches into compact per-instrument status ranges."""
    ordered = sorted(
        matches,
        key=lambda match: (
            str(match.get("instrumentKey") or ""),
            match.get("buyDate") or "9999-12-31",
            str(match.get("status") or ""),
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

    return [
        {
            "instrumentKey": group["instrumentKey"],
            "ticker": group["ticker"],
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
        }
        for group in groups
    ]
