# Future Sell Planner Design

Status: design only, not implemented.

This document captures future planner features for selling stocks/ETFs more intelligently:

1. Plan how to raise a fixed CZK amount now in the most tax-efficient way.
2. Plan a sale across more than one instrument.
3. Allow planner prices to be fetched from a market price endpoint instead of manually entered.

The current app remains local-first and statement-driven. These features should preserve that posture: imported historical activity stays local, price lookup is explicit, and any external API dependency is isolated behind a small provider interface.

## Goals

- Let the user ask: "I want to raise 150,000 CZK now; what should I sell?"
- Minimize estimated Czech taxable gain/tax impact, not just maximize proceeds.
- Support sales from one or many holdings.
- Use current market prices when available.
- Keep manual price entry as a fallback.
- Avoid overwhelming the UI with per-lot output; aggregate lots into readable ranges.
- Make optimization explainable: show why each suggested sale was chosen.

## Non-Goals

- Do not place trades.
- Do not modify broker account data.
- Do not guarantee real-time market accuracy.
- Do not optimize for portfolio strategy, risk, diversification, or investment advice unless later added explicitly.
- Do not treat this as final tax advice.

## User Workflows

### Fixed Proceeds Optimizer

The user enters:

- Target proceeds in CZK, for example `150000`.
- Sale date, default today.
- Optional instrument filter:
  - all holdings,
  - selected instruments,
  - one instrument.
- Price mode:
  - use fetched current prices,
  - use manually entered prices,
  - mixed, where missing fetched prices can be filled manually.
- Optimization mode:
  - lowest estimated tax,
  - lowest taxable gain,
  - preserve tax-free lots,
  - simple FIFO simulation for a chosen set.

The app returns:

- Suggested sell list:
  - instrument,
  - quantity to sell,
  - estimated proceeds,
  - estimated taxable gain,
  - estimated tax at 15%,
  - exempt proceeds,
  - remaining holding.
- Summary:
  - target proceeds,
  - estimated achieved proceeds,
  - shortfall/overage,
  - taxable gain delta,
  - tax delta,
  - total gross proceeds after plan.
- Explanation:
  - compact lot groups, earliest-to-latest,
  - taxable vs exempt ranges,
  - reason selected, for example "uses lots already past 3-year time test".

### Multi-Instrument Manual Plan

The user can add multiple sale rows:

| Instrument | Quantity | Price mode | Price/share CZK | Sale date |
| --- | ---: | --- | ---: | --- |
| VUAA | 1.25 | fetched | 2,850 | 2026-06-05 |
| XY7D | 4 | manual | 320 | 2026-06-05 |

The app evaluates the combined plan as one scenario, because the Czech annual gross proceeds exemption is global for the year. This matters: adding a second instrument can push gross proceeds above 100,000 CZK and make otherwise exempt short-term sales taxable.

### Price-Free Planner

In single-instrument and multi-instrument planner modes, price per share can be blank when price lookup is enabled.

Expected behavior:

- If price lookup succeeds, use the fetched price and show timestamp/source.
- If price lookup fails, keep the row invalid until the user enters a manual price.
- If the instrument cannot be mapped to a market symbol, show a mapping warning.

## Data Model Additions

### Price Quote

```json
{
  "instrumentKey": "ISIN:IE00BFMXXD54",
  "ticker": "VUAA",
  "isin": "IE00BFMXXD54",
  "currency": "EUR",
  "price": "112.34",
  "priceCzk": "2788.12",
  "fxRate": "24.82",
  "asOf": "2026-06-05T10:15:00Z",
  "provider": "example-provider",
  "marketStatus": "open"
}
```

Notes:

- Store prices as strings or `Decimal` internally, never floats.
- `priceCzk` can be provider-supplied or computed from `price * fxRate`.
- `asOf` must be visible in the UI so the user knows price freshness.

### Planned Sale Input

```json
{
  "saleDate": "2026-06-05",
  "targetProceedsCzk": "150000",
  "optimizationMode": "min_tax",
  "priceMode": "fetch",
  "rows": [
    {
      "instrumentKey": "ISIN:IE00BFMXXD54",
      "quantity": "",
      "pricePerShareCzk": "",
      "priceSource": "fetch"
    }
  ]
}
```

For a fixed-proceeds optimizer, `quantity` can be blank because the algorithm chooses quantities. For manual multi-instrument planning, `quantity` is required for each row.

### Planned Sale Output

```json
{
  "saleDate": "2026-06-05",
  "targetProceedsCzk": 150000.0,
  "estimatedProceedsCzk": 150012.34,
  "shortfallCzk": 0.0,
  "taxableGainDeltaCzk": 53009.0,
  "estimatedTaxDelta15Czk": 7951.0,
  "rows": [
    {
      "instrumentKey": "ISIN:IE00BFMXXD54",
      "ticker": "VUAA",
      "quantity": 1.23456789,
      "pricePerShareCzk": 2788.12,
      "estimatedProceedsCzk": 3442.31,
      "taxableGainCzk": 0.0,
      "taxStatusSummary": "Mostly exempt by 3-year time test"
    }
  ],
  "lotGroups": [
    {
      "instrumentKey": "ISIN:IE00BFMXXD54",
      "ticker": "VUAA",
      "taxable": true,
      "status": "Taxable: time test not met",
      "buyDateStart": "2024-07-23",
      "buyDateEnd": "2024-11-26",
      "lotCount": 82,
      "quantity": 1.01,
      "grossProceedsCzk": 148000.0,
      "costCzk": 94991.0,
      "gainCzk": 53009.0
    }
  ],
  "warnings": []
}
```

## Endpoint Design

### Price Quote Endpoint

```text
POST /api/prices/quote
```

Request:

```json
{
  "instruments": [
    {
      "instrumentKey": "ISIN:IE00BFMXXD54",
      "ticker": "VUAA",
      "isin": "IE00BFMXXD54"
    }
  ],
  "asOf": "2026-06-05"
}
```

Response:

```json
{
  "quotes": {
    "ISIN:IE00BFMXXD54": {
      "priceCzk": "2788.12",
      "currency": "EUR",
      "price": "112.34",
      "fxRate": "24.82",
      "asOf": "2026-06-05T10:15:00Z",
      "provider": "example-provider"
    }
  },
  "warnings": []
}
```

Implementation notes:

- Keep provider code behind an interface such as `PriceProvider`.
- Add a local cache with a short TTL, for example 5 to 15 minutes.
- The provider should not be called during every keystroke. Use an explicit "Refresh prices" action.
- If price lookup needs API keys, store them in `.invest_tax_calc/` or environment variables, not in git.

### Multi-Row Planning Endpoint

```text
POST /api/plan/batch
```

Request:

```json
{
  "reports": [],
  "rates": "EUR=24.82",
  "saleDate": "2026-06-05",
  "rows": [
    {
      "instrumentKey": "ISIN:IE00BFMXXD54",
      "quantity": "1.2",
      "pricePerShareCzk": "2788.12"
    },
    {
      "instrumentKey": "ISIN:LU0908500753",
      "quantity": "2",
      "pricePerShareCzk": "7411"
    }
  ]
}
```

Response:

Return the same structure as planned sale output. The backend should create one planned sell `Trade` per row, then run `analyze_transactions()` once with all planned sells appended.

### Fixed-Proceeds Optimizer Endpoint

```text
POST /api/plan/target-proceeds
```

Request:

```json
{
  "reports": [],
  "rates": "EUR=24.82",
  "saleDate": "2026-06-05",
  "targetProceedsCzk": "150000",
  "optimizationMode": "min_tax",
  "candidateInstrumentKeys": [
    "ISIN:IE00BFMXXD54",
    "ISIN:LU0908500753"
  ],
  "quotes": {
    "ISIN:IE00BFMXXD54": {
      "pricePerShareCzk": "2788.12"
    }
  }
}
```

Response:

Return the planned sale output plus optimizer metadata:

```json
{
  "optimizer": {
    "mode": "min_tax",
    "targetReached": true,
    "candidateCount": 27,
    "strategy": "lot-level greedy by tax cost per CZK proceeds"
  }
}
```

## Optimization Algorithm

Start conservative and explainable.

### Candidate Generation

For each open tax lot:

- instrument key,
- remaining quantity,
- estimated proceeds at current price,
- cost basis,
- tax-free date,
- taxable status on planned sale date,
- estimated taxable gain per unit,
- estimated tax per CZK proceeds.

Split large lots only as needed to hit the target proceeds. The app already tracks fractional quantities, so partial lot sales are acceptable.

### Greedy Baseline

For `min_tax`:

1. Prefer lots that are exempt by 3-year time test.
2. Then prefer taxable lots with the lowest taxable gain per CZK proceeds.
3. Then prefer lots with losses or low gains.
4. Stop when target proceeds is reached.
5. If the last lot overshoots, sell only the quantity needed from that lot.

This is simple and explainable. It may not be mathematically perfect when the annual 100,000 CZK gross exemption threshold interacts with existing sales, but it is a good first implementation.

### Gross Proceeds Threshold Handling

The annual 100,000 CZK gross proceeds rule is global. The optimizer must evaluate the complete scenario, not each instrument independently.

Important cases:

- If baseline annual gross proceeds are already above 100,000 CZK, short-term planned sales are taxable unless time-exempt.
- If the target sale pushes annual gross proceeds above 100,000 CZK, previous short-term sells in that year may become taxable too.
- Optimizer scoring should include total scenario delta, not just tax from the new planned rows.

Recommended first approach:

1. Generate a greedy plan.
2. Run full `analyze_transactions()` with the proposed planned sells.
3. Report total delta vs baseline.
4. Optionally try a few alternate greedy sort orders and pick the best total delta.

## UI Design

### Planner Modes

Use tabs or a segmented control:

- Single sale
- Multi-sale
- Target amount

### Single Sale

Keep current behavior, but price can be:

- manual,
- fetched.

When fetched:

- show price,
- source,
- timestamp,
- refresh button.

### Multi-Sale

Use an editable table:

| Instrument | Qty | Price | Source | Proceeds | Remove |
| --- | ---: | ---: | --- | ---: | --- |

Controls:

- Add row
- Refresh prices
- Run plan

### Target Amount

Inputs:

- target CZK proceeds,
- candidate instruments,
- sale date,
- optimization mode,
- refresh prices,
- run optimizer.

Output:

- compact recommended sales table,
- summary metrics,
- grouped taxable/exempt lot ranges,
- warnings.

Avoid rendering raw hundreds of tax lot rows by default. Offer an optional detail expander if needed.

## Price Provider Options

This design intentionally does not pick a provider yet. Provider choice should consider:

- coverage for EU ETFs and Trading 212 tickers,
- ISIN lookup support,
- rate limits,
- API key requirements,
- currency support,
- terms of service for local personal use.

Possible provider adapter interface:

```python
class PriceProvider:
    def quote_many(self, instruments: list[InstrumentRef]) -> dict[str, PriceQuote]:
        ...
```

Add one provider at a time, with fixtures/tests around response parsing.

## Caching

Price cache:

- path: `.invest_tax_calc/price_cache.json`
- key: provider + instrument key + date/time bucket
- TTL: configurable, default 5 to 15 minutes

Parsed PDF cache already exists separately and should remain independent of price cache.

## Testing Plan

Unit tests:

- multi-instrument planned sale crosses 100,000 CZK gross threshold,
- fixed-proceeds optimizer uses tax-free lots first,
- fixed-proceeds optimizer handles partial lot sale to hit target,
- price lookup fallback requires manual price when missing,
- stale price cache refreshes after TTL,
- lot group aggregation remains compact.

Integration tests:

- `/api/prices/quote` with fake provider,
- `/api/plan/batch`,
- `/api/plan/target-proceeds`.

UI tests:

- switch planner modes,
- add/remove multi-sale rows,
- refresh prices,
- run target optimizer,
- verify no flood of per-lot status cards.

## Open Questions

- Which price provider should be used for EU ETFs and Trading 212 instruments?
- Should target proceeds allow a tolerance, for example +/- 100 CZK?
- Should optimizer prefer selling whole shares when the instrument does not support fractional quantity?
- Should the app model broker minimum order size?
- Should price lookup use latest market price, close price, or user-selected date price?
- Should optimization include fees/spread estimates?
- Should the target optimizer allow "do not sell these instruments" constraints?
