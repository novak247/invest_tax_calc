from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class InstrumentRef:
    instrument_key: str
    ticker: str = ""
    isin: str = ""


@dataclass(frozen=True)
class PriceQuote:
    instrument_key: str
    price_czk: Decimal
    currency: str
    price: Decimal
    fx_rate: Decimal
    as_of: str
    provider: str
    market_status: str = ""

    def payload(self) -> dict[str, str]:
        return {
            "instrumentKey": self.instrument_key,
            "priceCzk": str(self.price_czk),
            "currency": self.currency,
            "price": str(self.price),
            "fxRate": str(self.fx_rate),
            "asOf": self.as_of,
            "provider": self.provider,
            "marketStatus": self.market_status,
        }


class PriceProvider(Protocol):
    name: str

    def quote_many(self, instruments: list[InstrumentRef]) -> dict[str, PriceQuote]:
        ...


class UnconfiguredPriceProvider:
    name = "unconfigured"

    def quote_many(self, instruments: list[InstrumentRef]) -> dict[str, PriceQuote]:
        return {}


class PriceCache:
    def __init__(self, path: Path, *, ttl_seconds: int = 600):
        self.path = path
        self.ttl_seconds = ttl_seconds

    def get(self, provider: str, instrument_key: str) -> PriceQuote | None:
        payload = self._read()
        entry = payload.get(self._key(provider, instrument_key))
        if not isinstance(entry, dict):
            return None
        if time.time() - float(entry.get("cachedAt") or 0) > self.ttl_seconds:
            return None
        try:
            return PriceQuote(
                instrument_key=str(entry["instrument_key"]),
                price_czk=Decimal(str(entry["price_czk"])),
                currency=str(entry["currency"]),
                price=Decimal(str(entry["price"])),
                fx_rate=Decimal(str(entry["fx_rate"])),
                as_of=str(entry["as_of"]),
                provider=str(entry["provider"]),
                market_status=str(entry.get("market_status") or ""),
            )
        except (KeyError, ValueError, InvalidOperation):
            return None

    def put(self, quote: PriceQuote) -> None:
        payload = self._read()
        payload[self._key(quote.provider, quote.instrument_key)] = {
            **asdict(quote),
            "price_czk": str(quote.price_czk),
            "price": str(quote.price),
            "fx_rate": str(quote.fx_rate),
            "cachedAt": time.time(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _read(self) -> dict[str, dict[str, object]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _key(provider: str, instrument_key: str) -> str:
        return f"{provider}:{instrument_key}"


def quote_instruments(
    instruments: list[InstrumentRef],
    *,
    provider: PriceProvider,
    cache: PriceCache,
) -> dict[str, object]:
    quotes: dict[str, PriceQuote] = {}
    missing: list[InstrumentRef] = []
    for instrument in instruments:
        cached = cache.get(provider.name, instrument.instrument_key)
        if cached is None:
            missing.append(instrument)
        else:
            quotes[instrument.instrument_key] = cached

    if missing:
        fetched = provider.quote_many(missing)
        for key, quote in fetched.items():
            quotes[key] = quote
            cache.put(quote)

    warnings: list[str] = []
    unresolved = [
        instrument.ticker or instrument.isin or instrument.instrument_key
        for instrument in instruments
        if instrument.instrument_key not in quotes
    ]
    if unresolved:
        if provider.name == "unconfigured":
            warnings.append(
                "No market price provider is configured. Enter CZK prices manually."
            )
        else:
            warnings.append(
                "No price was returned for: " + ", ".join(sorted(unresolved))
            )
    return {
        "quotes": {key: quote.payload() for key, quote in quotes.items()},
        "warnings": warnings,
    }
