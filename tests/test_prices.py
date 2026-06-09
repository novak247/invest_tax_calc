from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from invest_tax_calc.prices import (
    InstrumentRef,
    PriceCache,
    PriceQuote,
    quote_instruments,
)


class FakeProvider:
    name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def quote_many(self, instruments: list[InstrumentRef]) -> dict[str, PriceQuote]:
        self.calls += 1
        return {
            instrument.instrument_key: PriceQuote(
                instrument_key=instrument.instrument_key,
                price_czk=Decimal("2788.12"),
                currency="EUR",
                price=Decimal("112.34"),
                fx_rate=Decimal("24.82"),
                as_of=datetime.now(timezone.utc).isoformat(),
                provider=self.name,
                market_status="open",
            )
            for instrument in instruments
        }


class PriceCacheTest(unittest.TestCase):
    def test_quote_lookup_reuses_fresh_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = FakeProvider()
            cache = PriceCache(Path(temp_dir) / "prices.json", ttl_seconds=600)
            refs = [InstrumentRef("ISIN:TEST", ticker="ETF")]

            first = quote_instruments(refs, provider=provider, cache=cache)
            second = quote_instruments(refs, provider=provider, cache=cache)

            self.assertEqual(provider.calls, 1)
            self.assertEqual(first["quotes"]["ISIN:TEST"]["priceCzk"], "2788.12")
            self.assertEqual(second["quotes"]["ISIN:TEST"]["provider"], "fake")

    def test_stale_cache_refreshes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = FakeProvider()
            cache = PriceCache(Path(temp_dir) / "prices.json", ttl_seconds=-1)
            refs = [InstrumentRef("ISIN:TEST", ticker="ETF")]

            quote_instruments(refs, provider=provider, cache=cache)
            quote_instruments(refs, provider=provider, cache=cache)

            self.assertEqual(provider.calls, 2)


if __name__ == "__main__":
    unittest.main()
