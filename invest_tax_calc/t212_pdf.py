from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO

from .models import Money, Trade


BUY_WORDS = {"buy", "bought", "koupit", "nakup", "nákup"}
SELL_WORDS = {"sell", "sold", "prodat", "prodej"}


def parse_trading212_pdf(data: bytes, *, filename: str = "trading212.pdf") -> list[Trade]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - environment boundary
        raise RuntimeError("PDF parsing requires the pypdf package. Run `uv add pypdf`.") from exc

    reader = PdfReader(BytesIO(data))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    return parse_trading212_pdf_text(text, filename=filename)


def parse_trading212_pdf_text(
    text: str,
    *,
    filename: str = "trading212.pdf",
) -> list[Trade]:
    trades: list[Trade] = []
    for row_number, line in enumerate(_candidate_lines(text), start=1):
        trade = _parse_trade_line(line, filename=filename, row_number=row_number)
        if trade:
            trades.append(trade)

    trades.sort(key=lambda tx: (tx.traded_at, 0 if tx.kind == "buy" else 1, tx.source_id))
    return trades


# Older Trading 212 PDFs extract the timestamp with the date and time glued
# together ("2024-08-0212:02:20"); newer ones keep a space. Tolerate both and
# normalize to a single space so the token layout is stable downstream.
_LEAD_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[ \t]*(\d{2}:\d{2}:\d{2})\b(.*)$")


def _candidate_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw_line in text.splitlines():
        match = _LEAD_RE.match(raw_line.strip())
        if not match:
            continue
        rebuilt = f"{match.group(1)} {match.group(2)} {match.group(3)}"
        lines.append(re.sub(r"\s+", " ", rebuilt).strip())
    return lines


def _parse_trade_line(line: str, *, filename: str, row_number: int) -> Trade | None:
    tokens = line.split()
    if len(tokens) < 12:
        return None

    traded_at_text = f"{tokens[0]} {tokens[1]}"
    ticker = tokens[2]
    isin = tokens[3]
    instrument_currency = tokens[4]

    index = 5
    order_ids: list[str] = []
    while index < len(tokens) and tokens[index].isdigit():
        order_ids.append(tokens[index])
        index += 1
    if not order_ids or index >= len(tokens):
        return None

    direction_token = tokens[index]
    index += 1
    direction = _ascii_lower(direction_token)
    if direction in BUY_WORDS:
        kind = "buy"
    elif direction in SELL_WORDS:
        kind = "sell"
    else:
        return None

    if index + 2 >= len(tokens):
        return None

    quantity = _decimal_or_none(tokens[index])
    if quantity is None or quantity <= 0:
        return None
    price = tokens[index + 1]
    trade_value = tokens[index + 2]
    rest_tokens = tokens[index + 3 :]

    account_currency = _last_currency(rest_tokens) or instrument_currency
    account_value = _last_decimal(rest_tokens)
    if account_value is None or account_value == 0:
        account_value = _decimal_or_none(trade_value)
    if account_value is None:
        return None

    fees = []
    fx_fee = _fx_fee(rest_tokens, account_currency)
    if fx_fee and fx_fee != 0:
        fees.append(Money(abs(fx_fee), account_currency))

    return Trade(
        source="Trading 212 PDF",
        source_id=order_ids[-1] or f"{filename}:{row_number}",
        action=direction_token,
        kind=kind,
        traded_at=datetime.strptime(traded_at_text, "%Y-%m-%d %H:%M:%S"),
        instrument_key=f"ISIN:{isin}",
        ticker=ticker,
        isin=isin,
        quantity=abs(quantity),
        gross=Money(abs(account_value), account_currency),
        fees=tuple(fees),
        raw={
            "filename": filename,
            "line": line,
            "order_ids": " ".join(order_ids),
            "instrument_currency": instrument_currency,
            "price": price,
            "trade_value": trade_value,
        },
    )


def _last_currency(tokens: list[str]) -> str:
    for token in reversed(tokens):
        if re.fullmatch(r"[A-Z]{3}", token):
            return token
    return ""


def _last_decimal(tokens: list[str]) -> Decimal | None:
    for token in reversed(tokens):
        parsed = _decimal_or_none(token)
        if parsed is not None:
            return parsed
    return None


def _fx_fee(tokens: list[str], account_currency: str) -> Decimal:
    for index, token in enumerate(tokens):
        if token != account_currency:
            continue
        if index + 1 >= len(tokens):
            continue
        parsed = _decimal_or_none(tokens[index + 1])
        if parsed is not None:
            return parsed
    return Decimal("0")


def _decimal_or_none(value: str | None) -> Decimal | None:
    text = (value or "").strip()
    if not text or text == "-":
        return None

    text = text.replace("\u00a0", " ")
    text = re.sub(r"[^0-9,.\-()]", "", text)
    if not text or text == "-":
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


def _ascii_lower(value: str) -> str:
    return (
        value.strip()
        .lower()
        .replace("á", "a")
        .replace("č", "c")
        .replace("ď", "d")
        .replace("é", "e")
        .replace("ě", "e")
        .replace("í", "i")
        .replace("ň", "n")
        .replace("ó", "o")
        .replace("ř", "r")
        .replace("š", "s")
        .replace("ť", "t")
        .replace("ú", "u")
        .replace("ů", "u")
        .replace("ý", "y")
        .replace("ž", "z")
    )
