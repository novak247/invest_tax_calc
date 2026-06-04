from __future__ import annotations

import csv
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import StringIO

from .models import Money, Trade


ORDER_ACTION_WORDS = ("buy", "sell")
FEE_WORDS = ("fee", "commission", "duty", "levy", "transactiontax")
IGNORED_FEE_WORDS = ("withholdingtax", "currency", "exchange")


def parse_trading212_csv(
    text: str,
    *,
    filename: str = "trading212.csv",
    default_currency: str = "CZK",
) -> list[Trade]:
    cleaned = text.lstrip("\ufeff").replace("\r\n", "\n")
    dialect = _sniff_dialect(cleaned)
    rows = csv.DictReader(StringIO(cleaned), dialect=dialect)
    trades: list[Trade] = []

    for row_number, row in enumerate(rows, start=2):
        normalized = {_norm(k): (k, (v or "").strip()) for k, v in row.items() if k}
        action = _pick(normalized, "action", "type", "eventtype", "transactiontype")
        action_lower = action.lower()
        if not any(word in action_lower for word in ORDER_ACTION_WORDS):
            continue

        kind = "buy" if "buy" in action_lower else "sell"
        quantity = _decimal_or_none(
            _pick(
                normalized,
                "noofshares",
                "numberofshares",
                "shares",
                "quantity",
                "filledquantity",
                "qty",
            )
        )
        if quantity is None or quantity <= 0:
            continue

        traded_at = _parse_datetime(
            _pick(normalized, "time", "date", "tradedate", "executiontime", "created")
        )
        ticker = _pick(normalized, "ticker", "symbol", "instrument", "shortname")
        isin = _pick(normalized, "isin")
        name = _pick(normalized, "name", "instrumentname", "company", "security")
        instrument_key = _instrument_key(isin, ticker, name)

        total = _decimal_or_none(
            _pick(
                normalized,
                "total",
                "value",
                "amount",
                "orderamount",
                "turnover",
                "consideration",
            )
        )
        total_currency = _currency(
            _pick(
                normalized,
                "currencytotal",
                "totalcurrency",
                "currencyamount",
                "amountcurrency",
                "currency",
            )
            or default_currency
        )

        if total is None:
            price = _decimal_or_none(
                _pick(normalized, "pricepershare", "price", "executionprice", "averageprice")
            )
            if price is None:
                continue
            total = price * quantity
            total_currency = _currency(
                _pick(
                    normalized,
                    "currencypricepershare",
                    "pricecurrency",
                    "currencyprice",
                    "currency",
                )
                or total_currency
            )

        fees = _extract_fees(row, normalized, default_currency=total_currency)
        trades.append(
            Trade(
                source="Trading 212",
                source_id=_pick(normalized, "id", "orderid", "transactionid") or f"{filename}:{row_number}",
                action=action or kind,
                kind=kind,
                traded_at=traded_at,
                instrument_key=instrument_key,
                ticker=ticker,
                isin=isin,
                name=name,
                quantity=abs(quantity),
                gross=Money(abs(total), total_currency),
                fees=tuple(fees),
                raw={str(k): str(v) for k, v in row.items() if k is not None},
            )
        )

    trades.sort(key=lambda tx: (tx.traded_at, 0 if tx.kind == "buy" else 1, tx.source_id))
    return trades


def _sniff_dialect(text: str) -> csv.Dialect:
    sample = text[:4096]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        return csv.excel


def _extract_fees(
    row: dict[str, str],
    normalized: dict[str, tuple[str, str]],
    *,
    default_currency: str,
) -> list[Money]:
    fees: list[Money] = []
    for column, raw_value in row.items():
        key = _norm(column)
        if not key:
            continue
        if any(word in key for word in IGNORED_FEE_WORDS):
            continue
        if not any(word in key for word in FEE_WORDS):
            continue

        amount = _decimal_or_none(raw_value or "")
        if amount is None or amount == 0:
            continue

        currency = _currency(_fee_currency(column, normalized) or default_currency)
        fees.append(Money(abs(amount), currency))
    return fees


def _fee_currency(column: str, normalized: dict[str, tuple[str, str]]) -> str:
    candidates = [
        f"currency({column})",
        f"currency {column}",
        f"{column} currency",
        f"currency{column}",
    ]
    for candidate in candidates:
        value = _pick(normalized, candidate)
        if value:
            return value
    return ""


def _pick(normalized: dict[str, tuple[str, str]], *names: str) -> str:
    for name in names:
        key = _norm(name)
        if key in normalized:
            return normalized[key][1]
    return ""


def _norm(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


def _currency(value: str) -> str:
    value = (value or "CZK").strip().upper()
    match = re.search(r"[A-Z]{3}", value)
    return match.group(0) if match else value[:3]


def _instrument_key(isin: str, ticker: str, name: str) -> str:
    if isin.strip():
        return f"ISIN:{isin.strip().upper()}"
    if ticker.strip():
        return f"TICKER:{ticker.strip().upper()}"
    return f"NAME:{name.strip().upper() or 'UNKNOWN'}"


def _decimal_or_none(value: str | None) -> Decimal | None:
    text = (value or "").strip()
    if not text:
        return None

    text = text.replace("\u00a0", " ").strip()
    text = re.sub(r"[^0-9,.\-()]", "", text)
    if not text:
        return None

    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")

    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")

    try:
        parsed = Decimal(text)
    except InvalidOperation:
        return None
    return -parsed if negative else parsed


def _parse_datetime(value: str) -> datetime:
    text = (value or "").strip()
    if not text:
        raise ValueError("Missing trade date/time in CSV row.")

    iso = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso)
        return parsed.replace(tzinfo=None)
    except ValueError:
        pass

    formats = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%d/%m/%Y %H:%M:%S",
        "%d.%m.%Y %H:%M:%S",
        "%d-%m-%Y %H:%M:%S",
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d.%m.%Y",
        "%d-%m-%Y",
    )
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unsupported trade date/time format: {value}")
