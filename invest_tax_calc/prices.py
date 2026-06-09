from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

PRICE_CACHE_VERSION = "price_cache_v1"
DEFAULT_PRICE_TTL_SECONDS = 600
_HTTP_TIMEOUT_SECONDS = 12
_USER_AGENT = "Mozilla/5.0 (compatible; InvestTaxCalc/0.1; local personal use)"


@dataclass(frozen=True)
class InstrumentRef:
    instrument_key: str
    ticker: str = ""
    isin: str = ""


@dataclass(frozen=True)
class PriceQuote:
    instrument_key: str
    ticker: str
    isin: str
    currency: str
    price: Decimal
    as_of: str
    provider: str
    market_status: str = ""
    symbol: str = ""

    def payload(self, *, rates: dict[str, Decimal]) -> tuple[dict[str, Any], str | None]:
        """Build the JSON payload, converting to CZK with the user's FX table."""
        price, currency = _normalize_currency(self.price, self.currency)
        warning: str | None = None
        price_czk: Decimal | None = None
        fx_rate: Decimal | None = None
        if currency == "CZK":
            price_czk = price
            fx_rate = Decimal("1")
        else:
            fx_rate = rates.get(currency)
            if fx_rate is None:
                warning = (
                    f"No CZK FX rate for {currency} ({self.ticker or self.instrument_key}). "
                    f"Add {currency}=... in the FX rates box to use this quote."
                )
            else:
                price_czk = price * fx_rate

        return (
            {
                "instrumentKey": self.instrument_key,
                "ticker": self.ticker,
                "isin": self.isin,
                "currency": currency,
                "price": str(price),
                "priceCzk": str(price_czk) if price_czk is not None else "",
                "fxRate": str(fx_rate) if fx_rate is not None else "",
                "asOf": self.as_of,
                "provider": self.provider,
                "marketStatus": self.market_status,
                "symbol": self.symbol,
            },
            warning,
        )


def _normalize_currency(price: Decimal, currency: str) -> tuple[Decimal, str]:
    cleaned = (currency or "").strip()
    # London-listed quotes can arrive in pence (GBp/GBX).
    if cleaned.upper() == "GBX" or cleaned == "GBp":
        return price / Decimal("100"), "GBP"
    return price, cleaned.upper()


class PriceProvider:
    name = "base"

    def quote_many(
        self, instruments: list[InstrumentRef]
    ) -> tuple[dict[str, PriceQuote], list[str]]:
        raise NotImplementedError

    def fx_to_czk(self, currencies: list[str]) -> tuple[dict[str, Decimal], list[str]]:
        """Return CZK rates for the given currencies. Providers without FX
        support return nothing; the per-quote payload warning still fires."""
        return {}, []


class YahooPriceProvider(PriceProvider):
    """Best-effort quotes from Yahoo Finance public endpoints (no API key).

    Symbols are resolved from the ISIN via the Yahoo search endpoint, with the
    raw ticker as a fallback. ``fetch_json`` is injectable for tests.
    """

    name = "yahoo"

    def __init__(self, fetch_json: Callable[[str], dict[str, Any]] | None = None):
        self._fetch_json = fetch_json or _http_get_json
        self._symbol_cache: dict[str, str] = {}

    def quote_many(
        self, instruments: list[InstrumentRef]
    ) -> tuple[dict[str, PriceQuote], list[str]]:
        quotes: dict[str, PriceQuote] = {}
        warnings: list[str] = []
        for instrument in instruments:
            label = instrument.ticker or instrument.isin or instrument.instrument_key
            try:
                quote = self._quote_one(instrument)
            except Exception as exc:
                warnings.append(f"Price lookup failed for {label}: {exc}")
                continue
            if quote is None:
                warnings.append(
                    f"Could not map {label} to a market symbol. Enter the price manually."
                )
                continue
            quotes[instrument.instrument_key] = quote
        return quotes, warnings

    def _quote_one(self, instrument: InstrumentRef) -> PriceQuote | None:
        for symbol in self._candidate_symbols(instrument):
            chart = self._chart_meta(symbol)
            if chart is None:
                continue
            price = chart.get("regularMarketPrice")
            currency = str(chart.get("currency") or "")
            if price is None or not currency:
                continue
            market_time = chart.get("regularMarketTime")
            as_of = (
                datetime.fromtimestamp(int(market_time), tz=timezone.utc).isoformat()
                if market_time
                else datetime.now(tz=timezone.utc).isoformat()
            )
            self._symbol_cache[instrument.instrument_key] = symbol
            price_decimal, currency = _yahoo_price(price, currency)
            return PriceQuote(
                instrument_key=instrument.instrument_key,
                ticker=instrument.ticker,
                isin=instrument.isin,
                currency=currency,
                price=price_decimal,
                as_of=as_of,
                provider=self.name,
                market_status=str(chart.get("marketState") or ""),
                symbol=symbol,
            )
        return None

    def _candidate_symbols(self, instrument: InstrumentRef) -> list[str]:
        cached = self._symbol_cache.get(instrument.instrument_key)
        symbols: list[str] = [cached] if cached else []
        if instrument.isin:
            symbols.extend(self._search_symbols(instrument.isin))
        if instrument.ticker:
            symbols.append(instrument.ticker)
        seen: set[str] = set()
        ordered: list[str] = []
        for symbol in symbols:
            if symbol and symbol not in seen:
                seen.add(symbol)
                ordered.append(symbol)
        return ordered

    def _search_symbols(self, isin: str) -> list[str]:
        url = (
            "https://query2.finance.yahoo.com/v1/finance/search?"
            + urllib.parse.urlencode({"q": isin, "quotesCount": 6, "newsCount": 0})
        )
        try:
            data = self._fetch_json(url)
        except Exception:
            return []
        results = data.get("quotes")
        if not isinstance(results, list):
            return []
        return [str(item.get("symbol")) for item in results if isinstance(item, dict) and item.get("symbol")]

    def fx_to_czk(self, currencies: list[str]) -> tuple[dict[str, Decimal], list[str]]:
        rates: dict[str, Decimal] = {}
        warnings: list[str] = []
        usd_czk: Decimal | None = None
        for currency in currencies:
            rate = self._chart_price(f"{currency}CZK=X")
            if rate is None:
                # Yahoo has no direct pair for exotics like MXNCZK; cross via USD
                # ("MXN=X" is USD->MXN, "CZK=X" is USD->CZK).
                if usd_czk is None:
                    usd_czk = self._chart_price("CZK=X")
                usd_to_currency = self._chart_price(f"{currency}=X")
                if usd_czk and usd_to_currency:
                    rate = (usd_czk / usd_to_currency).quantize(Decimal("0.000001"))
            if rate is None or rate <= 0:
                warnings.append(f"Could not fetch a CZK FX rate for {currency}.")
                continue
            rates[currency] = rate
        return rates, warnings

    def _chart_price(self, symbol: str) -> Decimal | None:
        meta = self._chart_meta(symbol)
        price = meta.get("regularMarketPrice") if meta else None
        if price is None:
            return None
        try:
            rate = Decimal(str(price))
        except InvalidOperation:
            return None
        return rate if rate > 0 else None

    def _chart_meta(self, symbol: str) -> dict[str, Any] | None:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
            "?interval=1d&range=1d"
        )
        try:
            data = self._fetch_json(url)
        except Exception:
            return None
        chart = data.get("chart") if isinstance(data, dict) else None
        results = chart.get("result") if isinstance(chart, dict) else None
        if not isinstance(results, list) or not results:
            return None
        meta = results[0].get("meta") if isinstance(results[0], dict) else None
        return meta if isinstance(meta, dict) else None


def _yahoo_price(raw_price: Any, currency: str) -> tuple[Decimal, str]:
    price = Decimal(str(raw_price))
    # Yahoo reports LSE quotes in pence with currency "GBp".
    if currency.strip() in {"GBp", "GBX"}:
        return price / Decimal("100"), "GBP"
    return price, currency.strip().upper()


def _http_get_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Unexpected provider response.")
    return data


class PriceCache:
    """Small JSON file cache so refresh clicks do not hammer the provider."""

    def __init__(
        self,
        path: Path,
        *,
        ttl_seconds: int = DEFAULT_PRICE_TTL_SECONDS,
        now_fn: Callable[[], float] = time.time,
    ):
        self.path = path
        self.ttl_seconds = ttl_seconds
        self._now = now_fn
        self._entries: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict) or payload.get("version") != PRICE_CACHE_VERSION:
            return {}
        entries = payload.get("entries")
        return entries if isinstance(entries, dict) else {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": PRICE_CACHE_VERSION, "entries": self._entries}
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def _key(self, provider: str, instrument_key: str) -> str:
        return f"{provider}::{instrument_key}"

    def get(self, provider: str, instrument_key: str) -> PriceQuote | None:
        entry = self._entries.get(self._key(provider, instrument_key))
        if not isinstance(entry, dict):
            return None
        cached_at = entry.get("cachedAtUnix")
        if not isinstance(cached_at, (int, float)):
            return None
        if self._now() - cached_at > self.ttl_seconds:
            return None
        try:
            return PriceQuote(
                instrument_key=str(entry["instrumentKey"]),
                ticker=str(entry.get("ticker") or ""),
                isin=str(entry.get("isin") or ""),
                currency=str(entry["currency"]),
                price=Decimal(str(entry["price"])),
                as_of=str(entry["asOf"]),
                provider=str(entry["provider"]),
                market_status=str(entry.get("marketStatus") or ""),
                symbol=str(entry.get("symbol") or ""),
            )
        except (KeyError, InvalidOperation, ValueError):
            return None

    def _fx_key(self, provider: str, currency: str) -> str:
        return f"{provider}::fx::{currency}"

    def get_fx(self, provider: str, currency: str) -> Decimal | None:
        entry = self._entries.get(self._fx_key(provider, currency))
        if not isinstance(entry, dict):
            return None
        cached_at = entry.get("cachedAtUnix")
        if not isinstance(cached_at, (int, float)):
            return None
        if self._now() - cached_at > self.ttl_seconds:
            return None
        try:
            return Decimal(str(entry["rateCzk"]))
        except (KeyError, InvalidOperation, ValueError):
            return None

    def set_fx(self, provider: str, currency: str, rate: Decimal) -> None:
        self._entries[self._fx_key(provider, currency)] = {
            "currency": currency,
            "rateCzk": str(rate),
            "cachedAtUnix": self._now(),
        }

    def set(self, quote: PriceQuote) -> None:
        self._entries[self._key(quote.provider, quote.instrument_key)] = {
            "instrumentKey": quote.instrument_key,
            "ticker": quote.ticker,
            "isin": quote.isin,
            "currency": quote.currency,
            "price": str(quote.price),
            "asOf": quote.as_of,
            "provider": quote.provider,
            "marketStatus": quote.market_status,
            "symbol": quote.symbol,
            "cachedAtUnix": self._now(),
        }


def quote_instruments(
    instruments: list[InstrumentRef],
    *,
    provider: PriceProvider,
    rates: dict[str, Decimal],
    cache: PriceCache | None = None,
    force_refresh: bool = False,
) -> tuple[dict[str, dict[str, Any]], list[str], dict[str, str]]:
    """Fetch quotes, serving fresh cache entries first, and convert to CZK.

    Currencies missing from the user's FX table are fetched from the provider
    (and cached) so quotes in CHF/MXN/etc. still get a ``priceCzk``. Returns
    ``(quotes, warnings, fetched_fx_rates)``.
    """
    warnings: list[str] = []
    quotes: dict[str, PriceQuote] = {}
    missing: list[InstrumentRef] = []

    for instrument in instruments:
        cached = None
        if cache is not None and not force_refresh:
            cached = cache.get(provider.name, instrument.instrument_key)
        if cached is not None:
            quotes[instrument.instrument_key] = cached
        else:
            missing.append(instrument)

    if missing:
        fetched, fetch_warnings = provider.quote_many(missing)
        warnings.extend(fetch_warnings)
        quotes.update(fetched)
        if cache is not None:
            for quote in fetched.values():
                cache.set(quote)
            if fetched:
                cache.save()

    effective_rates = dict(rates)
    fetched_fx: dict[str, Decimal] = {}
    needed_currencies = sorted(
        {_normalize_currency(q.price, q.currency)[1] for q in quotes.values()}
        - {"CZK"}
        - set(effective_rates)
    )
    missing_fx: list[str] = []
    for currency in needed_currencies:
        cached_rate = None
        if cache is not None and not force_refresh:
            cached_rate = cache.get_fx(provider.name, currency)
        if cached_rate is not None:
            fetched_fx[currency] = cached_rate
        else:
            missing_fx.append(currency)
    if missing_fx:
        fx_rates, fx_warnings = provider.fx_to_czk(missing_fx)
        warnings.extend(fx_warnings)
        fetched_fx.update(fx_rates)
        if cache is not None and fx_rates:
            for currency, rate in fx_rates.items():
                cache.set_fx(provider.name, currency, rate)
            cache.save()
    effective_rates.update(fetched_fx)

    payload: dict[str, dict[str, Any]] = {}
    for instrument_key, quote in quotes.items():
        quote_payload, warning = quote.payload(rates=effective_rates)
        if warning:
            warnings.append(warning)
        currency = quote_payload["currency"]
        if currency in fetched_fx:
            quote_payload["fxSource"] = "fetched"
        else:
            quote_payload["fxSource"] = "user" if currency != "CZK" and quote_payload["fxRate"] else ""
        payload[instrument_key] = quote_payload
    return payload, warnings, {cur: str(rate) for cur, rate in fetched_fx.items()}
