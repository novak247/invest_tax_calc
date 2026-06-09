from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import app
from invest_tax_calc.models import Money, Trade
from invest_tax_calc.prices import InstrumentRef, PriceCache, PriceQuote


class FakePriceProvider:
    name = "fake"

    def quote_many(self, instruments: list[InstrumentRef]) -> dict[str, PriceQuote]:
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


class PlannerApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_provider = app.PRICE_PROVIDER
        self.old_cache = app.PRICE_CACHE
        self.old_parse_reports = app.Handler._parse_report_transactions
        app.PRICE_PROVIDER = FakePriceProvider()
        app.PRICE_CACHE = PriceCache(Path(self.temp_dir.name) / "prices.json")
        app.Handler._parse_report_transactions = lambda _self, _payload: [
            Trade(
                source="test",
                source_id="old-buy",
                action="buy",
                kind="buy",
                traded_at=datetime.fromisoformat("2020-01-01T10:00:00"),
                instrument_key="ISIN:OLD",
                ticker="OLD",
                isin="OLD",
                quantity=Decimal("2"),
                gross=Money(Decimal("100000"), "CZK"),
            ),
            Trade(
                source="test",
                source_id="new-buy",
                action="buy",
                kind="buy",
                traded_at=datetime.fromisoformat("2025-01-01T10:00:00"),
                instrument_key="ISIN:NEW",
                ticker="NEW",
                isin="NEW",
                quantity=Decimal("2"),
                gross=Money(Decimal("100000"), "CZK"),
            ),
        ]
        self.server = app.ReloadFriendlyHTTPServer(("127.0.0.1", 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        app.PRICE_PROVIDER = self.old_provider
        app.PRICE_CACHE = self.old_cache
        app.Handler._parse_report_transactions = self.old_parse_reports
        self.temp_dir.cleanup()

    def test_planner_ui_and_price_quote_endpoint(self) -> None:
        with urllib.request.urlopen(self.base_url + "/", timeout=2) as response:
            page = response.read().decode("utf-8")
        self.assertIn('id="multiPlannerPane"', page)
        self.assertIn('id="targetPlannerPane"', page)

        request = urllib.request.Request(
            self.base_url + "/api/prices/quote",
            data=json.dumps(
                {
                    "instruments": [
                        {
                            "instrumentKey": "ISIN:TEST",
                            "ticker": "ETF",
                            "isin": "TEST",
                        }
                    ]
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(payload["quotes"]["ISIN:TEST"]["priceCzk"], "2788.12")
        self.assertEqual(payload["quotes"]["ISIN:TEST"]["provider"], "fake")

    def test_batch_and_target_planner_endpoints(self) -> None:
        batch = self._post(
            "/api/plan/batch",
            {
                "saleDate": "2026-06-01",
                "rows": [
                    {
                        "instrumentKey": "ISIN:OLD",
                        "quantity": "1",
                        "pricePerShareCzk": "100000",
                    },
                    {
                        "instrumentKey": "ISIN:NEW",
                        "quantity": "1",
                        "pricePerShareCzk": "100000",
                    },
                ],
            },
        )
        target = self._post(
            "/api/plan/target-proceeds",
            {
                "saleDate": "2026-06-01",
                "targetProceedsCzk": "150000",
                "optimizationMode": "min_tax",
                "candidateInstrumentKeys": ["ISIN:OLD", "ISIN:NEW"],
                "quotes": {
                    "ISIN:OLD": {"pricePerShareCzk": "100000"},
                    "ISIN:NEW": {"pricePerShareCzk": "100000"},
                },
            },
        )

        self.assertEqual(batch["estimatedProceedsCzk"], 200000.0)
        self.assertEqual(len(batch["rows"]), 2)
        self.assertTrue(target["optimizer"]["targetReached"])
        self.assertEqual(target["rows"][0]["instrumentKey"], "ISIN:OLD")

    def _post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
