from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from invest_tax_calc.prices import (
    InstrumentRef,
    PriceCache,
    PriceProvider,
    PriceQuote,
    YahooPriceProvider,
    quote_instruments,
)

VUAA = InstrumentRef("ISIN:IE00BFMXXD54", ticker="VUAA", isin="IE00BFMXXD54")


def quote(key: str, price: str, currency: str = "EUR") -> PriceQuote:
    return PriceQuote(
        instrument_key=key,
        ticker="VUAA",
        isin="IE00BFMXXD54",
        currency=currency,
        price=Decimal(price),
        as_of="2026-06-05T10:15:00+00:00",
        provider="fake",
    )


class FakeProvider(PriceProvider):
    name = "fake"

    def __init__(self, quotes: dict[str, PriceQuote]):
        self.quotes = quotes
        self.calls = 0

    def quote_many(self, instruments):
        self.calls += 1
        found = {
            ref.instrument_key: self.quotes[ref.instrument_key]
            for ref in instruments
            if ref.instrument_key in self.quotes
        }
        warnings = [
            f"Could not map {ref.ticker or ref.instrument_key} to a market symbol."
            for ref in instruments
            if ref.instrument_key not in self.quotes
        ]
        return found, warnings


class QuoteInstrumentsTest(unittest.TestCase):
    def test_converts_to_czk_with_fx_rates(self) -> None:
        provider = FakeProvider({VUAA.instrument_key: quote(VUAA.instrument_key, "112.34")})
        quotes, warnings = quote_instruments(
            [VUAA], provider=provider, rates={"EUR": Decimal("24.82")}
        )

        payload = quotes[VUAA.instrument_key]
        self.assertEqual(payload["price"], "112.34")
        self.assertEqual(payload["fxRate"], "24.82")
        self.assertEqual(payload["priceCzk"], "2788.2788")
        self.assertEqual(payload["currency"], "EUR")
        self.assertEqual(warnings, [])

    def test_missing_fx_rate_warns_and_leaves_czk_blank(self) -> None:
        provider = FakeProvider({VUAA.instrument_key: quote(VUAA.instrument_key, "112.34")})
        quotes, warnings = quote_instruments([VUAA], provider=provider, rates={})

        self.assertEqual(quotes[VUAA.instrument_key]["priceCzk"], "")
        self.assertTrue(any("FX rate" in warning for warning in warnings))

    def test_pence_quotes_are_normalized_to_gbp(self) -> None:
        provider = FakeProvider(
            {VUAA.instrument_key: quote(VUAA.instrument_key, "2850", currency="GBp")}
        )
        quotes, _ = quote_instruments(
            [VUAA], provider=provider, rates={"GBP": Decimal("29.40")}
        )

        payload = quotes[VUAA.instrument_key]
        self.assertEqual(payload["currency"], "GBP")
        self.assertEqual(Decimal(payload["price"]), Decimal("28.5"))
        self.assertEqual(Decimal(payload["priceCzk"]), Decimal("837.90"))

    def test_unmapped_instrument_returns_warning(self) -> None:
        provider = FakeProvider({})
        quotes, warnings = quote_instruments([VUAA], provider=provider, rates={})

        self.assertEqual(quotes, {})
        self.assertTrue(any("Could not map" in warning for warning in warnings))


class PriceCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "price_cache.json"
        self.now = 1000.0

    def make_cache(self, ttl: int = 600) -> PriceCache:
        return PriceCache(self.path, ttl_seconds=ttl, now_fn=lambda: self.now)

    def test_fresh_entry_is_served_without_provider_call(self) -> None:
        provider = FakeProvider({VUAA.instrument_key: quote(VUAA.instrument_key, "112.34")})
        cache = self.make_cache()

        quote_instruments([VUAA], provider=provider, rates={}, cache=cache)
        self.assertEqual(provider.calls, 1)

        reloaded = self.make_cache()
        quote_instruments([VUAA], provider=provider, rates={}, cache=reloaded)
        self.assertEqual(provider.calls, 1)

    def test_stale_entry_refreshes_after_ttl(self) -> None:
        provider = FakeProvider({VUAA.instrument_key: quote(VUAA.instrument_key, "112.34")})
        cache = self.make_cache(ttl=600)

        quote_instruments([VUAA], provider=provider, rates={}, cache=cache)
        self.now += 601
        quote_instruments([VUAA], provider=provider, rates={}, cache=self.make_cache(ttl=600))

        self.assertEqual(provider.calls, 2)

    def test_force_refresh_skips_cache(self) -> None:
        provider = FakeProvider({VUAA.instrument_key: quote(VUAA.instrument_key, "112.34")})
        cache = self.make_cache()

        quote_instruments([VUAA], provider=provider, rates={}, cache=cache)
        quote_instruments(
            [VUAA], provider=provider, rates={}, cache=cache, force_refresh=True
        )

        self.assertEqual(provider.calls, 2)


class YahooProviderTest(unittest.TestCase):
    def test_parses_search_and_chart_responses(self) -> None:
        def fetch_json(url: str) -> dict:
            if "finance/search" in url:
                self.assertIn("IE00BFMXXD54", url)
                return {"quotes": [{"symbol": "VUAA.L"}]}
            if "finance/chart/VUAA.L" in url:
                return {
                    "chart": {
                        "result": [
                            {
                                "meta": {
                                    "regularMarketPrice": 9123.5,
                                    "currency": "GBp",
                                    "regularMarketTime": 1765000000,
                                    "marketState": "REGULAR",
                                }
                            }
                        ]
                    }
                }
            raise AssertionError(f"Unexpected URL: {url}")

        provider = YahooPriceProvider(fetch_json=fetch_json)
        quotes, warnings = provider.quote_many([VUAA])

        self.assertEqual(warnings, [])
        result = quotes[VUAA.instrument_key]
        self.assertEqual(result.currency, "GBP")
        self.assertEqual(result.price, Decimal("91.235"))
        self.assertEqual(result.symbol, "VUAA.L")
        self.assertEqual(result.market_status, "REGULAR")

    def test_unresolvable_symbol_warns(self) -> None:
        def fetch_json(url: str) -> dict:
            if "finance/search" in url:
                return {"quotes": []}
            return {"chart": {"result": []}}

        provider = YahooPriceProvider(fetch_json=fetch_json)
        quotes, warnings = provider.quote_many([VUAA])

        self.assertEqual(quotes, {})
        self.assertTrue(any("Could not map" in warning for warning in warnings))


class PriceQuoteEndpointTest(unittest.TestCase):
    def test_handle_price_quote_with_fake_provider(self) -> None:
        import app

        provider = FakeProvider({VUAA.instrument_key: quote(VUAA.instrument_key, "112.34")})
        with tempfile.TemporaryDirectory() as tmp:
            cache = PriceCache(Path(tmp) / "cache.json")
            result = app.handle_price_quote(
                {
                    "instruments": [
                        {
                            "instrumentKey": VUAA.instrument_key,
                            "ticker": "VUAA",
                            "isin": "IE00BFMXXD54",
                        }
                    ],
                    "rates": "EUR=24.82",
                },
                provider=provider,
                cache=cache,
            )

        self.assertEqual(
            result["quotes"][VUAA.instrument_key]["priceCzk"], "2788.2788"
        )
        self.assertEqual(result["warnings"], [])

    def test_handle_price_quote_requires_instruments(self) -> None:
        import app

        with self.assertRaises(app.AppError):
            app.handle_price_quote({"instruments": []})


if __name__ == "__main__":
    unittest.main()
