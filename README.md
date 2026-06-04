# Invest Tax Calc

Local-first Czech tax helper for Trading 212 stock and ETF activity.

This is an MVP for personal use. It imports a Trading 212 CSV export, matches sell orders against buy lots, and estimates which realized gains are taxable under the Czech securities rules:

- annual gross sale proceeds up to 100,000 CZK are treated as exempt,
- otherwise, lots held for at least 3 years are treated as exempt,
- non-exempt lots are included in the estimated Section 10 taxable result.

The app is not tax advice. Use it as a reconciliation helper and keep the generated results with your broker statements.

## Run

```powershell
uv run python app.py
```

Then open:

```text
http://127.0.0.1:8765
```

If port `8765` is busy:

```powershell
uv run python app.py --port 8766
```

For development hot reload:

```powershell
uv run python app.py --reload
```

`--reload` restarts the local server when Python or static web files change. The browser page also refreshes itself after the restarted server is back.

## Trading 212 Data

In Trading 212, export account history as CSV with orders included. The parser focuses on buy and sell rows and ignores dividends, deposits, withdrawals, and interest for now.

If your report is not in CZK, fill in the FX rates box before analyzing. For Czech taxes, you will usually want consistent CZK conversion rates for the tax year, not necessarily the broker's execution FX rate.

Supported rate formats:

```text
USD=23.20
EUR=25.10
USD:2025=23.42
EUR:2025=25.05
```

Year-specific entries such as `USD:2025` override a generic `USD` rate.

## Current Scope

- Trading 212 CSV import
- Stocks and ETFs only
- FIFO lot matching
- Czech 100,000 CZK annual gross proceeds exemption
- Czech 3-year time test
- Sell planner based on current uploaded holdings

## Future Ideas

- XTB Excel import
- Trading 212 API report download
- Dividends and withholding tax worksheet
- CNB/GFR annual FX-rate import
- SQLite persistence and saved reports
## Gmail Report Import

The email connector is local-first and requests only read-only Gmail access. It does not expose any option for write, send, delete, move, label, or modify mail scopes.

The web app has a Gmail import panel in the sidebar. Create a Google OAuth desktop client, then either paste the client ID into the app or set:

```powershell
$env:GMAIL_CLIENT_ID="your-client-id.apps.googleusercontent.com"
```

If Google gives your desktop client a secret and token exchange requires it, also set:

```powershell
$env:GMAIL_CLIENT_SECRET="your-client-secret"
```

Run:

```powershell
uv run python -m invest_tax_calc.email_import --query "from:trading212 has:attachment"
```

The Gmail connector requests exactly:

```text
https://www.googleapis.com/auth/gmail.readonly
```

Attachments are downloaded into `imports/email_reports/`. Imported attachment hashes are recorded in `.invest_tax_calc/email_import_ledger.json`, so re-running an import skips duplicates by SHA-256 hash.

When a downloaded attachment is a CSV, the web app loads the first matching CSV into the analyzer automatically. If the CSV uses non-CZK values, keep the FX rates box filled before importing.
