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

    def __init__(self, quotes: dict[str, PriceQuote], fx: dict[str, Decimal] | None = None):
        self.quotes = quotes
        self.fx = fx or {}
        self.calls = 0
        self.fx_calls = 0

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

    def fx_to_czk(self, currencies):
        self.fx_calls += 1
        found = {currency: self.fx[currency] for currency in currencies if currency in self.fx}
        warnings = [
            f"Could not fetch a CZK FX rate for {currency}."
            for currency in currencies
            if currency not in self.fx
        ]
        return found, warnings


class QuoteInstrumentsTest(unittest.TestCase):
    def test_converts_to_czk_with_fx_rates(self) -> None:
        provider = FakeProvider({VUAA.instrument_key: quote(VUAA.instrument_key, "112.34")})
        quotes, warnings, fx = quote_instruments(
            [VUAA], provider=provider, rates={"EUR": Decimal("24.82")}
        )

        payload = quotes[VUAA.instrument_key]
        self.assertEqual(payload["price"], "112.34")
        self.assertEqual(payload["fxRate"], "24.82")
        self.assertEqual(payload["priceCzk"], "2788.2788")
        self.assertEqual(payload["currency"], "EUR")
        self.assertEqual(payload["fxSource"], "user")
        self.assertEqual(warnings, [])
        self.assertEqual(fx, {})

    def test_missing_fx_rate_is_fetched_from_provider(self) -> None:
        provider = FakeProvider(
            {VUAA.instrument_key: quote(VUAA.instrument_key, "112.34")},
            fx={"EUR": Decimal("24.82")},
        )
        quotes, warnings, fx = quote_instruments([VUAA], provider=provider, rates={})

        payload = quotes[VUAA.instrument_key]
        self.assertEqual(payload["priceCzk"], "2788.2788")
        self.assertEqual(payload["fxRate"], "24.82")
        self.assertEqual(payload["fxSource"], "fetched")
        self.assertEqual(fx, {"EUR": "24.82"})
        self.assertEqual(warnings, [])

    def test_user_rates_win_over_fetched_fx(self) -> None:
        provider = FakeProvider(
            {VUAA.instrument_key: quote(VUAA.instrument_key, "100")},
            fx={"EUR": Decimal("99")},
        )
        quotes, _, fx = quote_instruments(
            [VUAA], provider=provider, rates={"EUR": Decimal("25")}
        )

        self.assertEqual(quotes[VUAA.instrument_key]["priceCzk"], "2500")
        self.assertEqual(provider.fx_calls, 0)
        self.assertEqual(fx, {})

    def test_missing_fx_rate_warns_and_leaves_czk_blank(self) -> None:
        provider = FakeProvider({VUAA.instrument_key: quote(VUAA.instrument_key, "112.34")})
        quotes, warnings, _ = quote_instruments([VUAA], provider=provider, rates={})

        self.assertEqual(quotes[VUAA.instrument_key]["priceCzk"], "")
        self.assertTrue(any("FX rate" in warning for warning in warnings))

    def test_pence_quotes_are_normalized_to_gbp(self) -> None:
        provider = FakeProvider(
            {VUAA.instrument_key: quote(VUAA.instrument_key, "2850", currency="GBp")}
        )
        quotes, _, _ = quote_instruments(
            [VUAA], provider=provider, rates={"GBP": Decimal("29.40")}
        )

        payload = quotes[VUAA.instrument_key]
        self.assertEqual(payload["currency"], "GBP")
        self.assertEqual(Decimal(payload["price"]), Decimal("28.5"))
        self.assertEqual(Decimal(payload["priceCzk"]), Decimal("837.90"))

    def test_pence_quotes_fetch_gbp_fx_not_gbp_pence(self) -> None:
        provider = FakeProvider(
            {VUAA.instrument_key: quote(VUAA.instrument_key, "2850", currency="GBp")},
            fx={"GBP": Decimal("29.40")},
        )
        quotes, warnings, fx = quote_instruments([VUAA], provider=provider, rates={})

        self.assertEqual(Decimal(quotes[VUAA.instrument_key]["priceCzk"]), Decimal("837.90"))
        self.assertEqual(fx, {"GBP": "29.40"})
        self.assertEqual(warnings, [])

    def test_unmapped_instrument_returns_warning(self) -> None:
        provider = FakeProvider({})
        quotes, warnings, _ = quote_instruments([VUAA], provider=provider, rates={})

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

    def test_fetched_fx_rate_is_cached(self) -> None:
        provider = FakeProvider(
            {VUAA.instrument_key: quote(VUAA.instrument_key, "112.34")},
            fx={"EUR": Decimal("24.82")},
        )
        quote_instruments([VUAA], provider=provider, rates={}, cache=self.make_cache())
        self.assertEqual(provider.fx_calls, 1)

        # Quote comes from cache too, so neither provider call repeats.
        quote_instruments([VUAA], provider=provider, rates={}, cache=self.make_cache())
        self.assertEqual(provider.fx_calls, 1)

        self.now += 601
        quote_instruments([VUAA], provider=provider, rates={}, cache=self.make_cache())
        self.assertEqual(provider.fx_calls, 2)

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

    def test_fx_to_czk_uses_chart_endpoint(self) -> None:
        def chart(price: float) -> dict:
            return {"chart": {"result": [{"meta": {"regularMarketPrice": price}}]}}

        def fetch_json(url: str) -> dict:
            if "finance/chart/CHFCZK" in url:
                return chart(27.15)
            # No direct pair for MXN -> cross via USD.
            if "finance/chart/MXNCZK" in url or "finance/chart/XXX" in url:
                return {"chart": {"result": []}}
            if "finance/chart/CZK%3DX" in url:
                return chart(21.0)
            if "finance/chart/MXN%3DX" in url:
                return chart(17.5)
            raise AssertionError(f"Unexpected URL: {url}")

        provider = YahooPriceProvider(fetch_json=fetch_json)
        rates, warnings = provider.fx_to_czk(["CHF", "MXN", "XXX"])

        self.assertEqual(rates["CHF"], Decimal("27.15"))
        self.assertEqual(rates["MXN"], Decimal("1.2"))
        self.assertTrue(any("XXX" in warning for warning in warnings))
        self.assertFalse(any("MXN" in warning for warning in warnings))

    def test_prefers_primary_listing_over_regional_cross_listing(self) -> None:
        tsm = InstrumentRef("ISIN:US8740391003", ticker="TSM", isin="US8740391003")

        def chart(price: float, currency: str) -> dict:
            return {
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "regularMarketPrice": price,
                                "currency": currency,
                                "regularMarketTime": 1765000000,
                                "marketState": "REGULAR",
                            }
                        }
                    ]
                }
            }

        def fetch_json(url: str) -> dict:
            if "finance/search" in url:
                # Yahoo lists the Mexican cross-listing before the NYSE ADR.
                return {
                    "quotes": [
                        {"symbol": "TSM.MX", "exchange": "MEX", "exchDisp": "Mexico"},
                        {"symbol": "TSM", "exchange": "NYQ", "exchDisp": "NYSE"},
                    ]
                }
            if "finance/chart/TSM.MX" in url:
                return chart(3900.0, "MXN")
            if "finance/chart/TSM?" in url:
                return chart(210.5, "USD")
            raise AssertionError(f"Unexpected URL: {url}")

        provider = YahooPriceProvider(fetch_json=fetch_json)
        quotes, warnings = provider.quote_many([tsm])

        self.assertEqual(warnings, [])
        result = quotes[tsm.instrument_key]
        self.assertEqual(result.symbol, "TSM")
        self.assertEqual(result.currency, "USD")
        self.assertEqual(result.price, Decimal("210.5"))

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
        self.assertEqual(result["fxRates"], {})

    def test_handle_price_quote_fetches_missing_fx(self) -> None:
        import app

        provider = FakeProvider(
            {VUAA.instrument_key: quote(VUAA.instrument_key, "112.34")},
            fx={"EUR": Decimal("24.82")},
        )
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
                    ]
                },
                provider=provider,
                cache=cache,
            )

        self.assertEqual(result["fxRates"], {"EUR": "24.82"})
        self.assertEqual(
            result["quotes"][VUAA.instrument_key]["priceCzk"], "2788.2788"
        )
        self.assertEqual(result["quotes"][VUAA.instrument_key]["fxSource"], "fetched")

    def test_handle_price_quote_requires_instruments(self) -> None:
        import app

        with self.assertRaises(app.AppError):
            app.handle_price_quote({"instruments": []})


if __name__ == "__main__":
    unittest.main()
