# Invest Tax Calc

Local-first Czech tax helper for Trading 212 stock and ETF activity.

This is an MVP for personal use. It imports Trading 212 activity statement PDFs, matches sell orders against buy lots, and estimates which realized gains are taxable under the Czech securities rules:

- annual gross sale proceeds up to 100,000 CZK are treated as exempt,
- otherwise, lots held for at least 3 years are treated as exempt,
- non-exempt lots are included in the estimated Section 10 taxable result.

The app is not tax advice. Use it as a reconciliation helper and keep the generated results with your broker statements.

## Run

```powershell
uv run python app.py
```

The default run mode watches Python and static web files, restarts the local server when they change, and refreshes the browser page after the restarted server is back.

Then open:

```text
http://127.0.0.1:8765
```

If port `8765` is busy:

```powershell
uv run python app.py --port 8766
```

To run without hot reload:

```powershell
uv run python app.py --no-reload
```

## Trading 212 Data

In Trading 212, export/download activity statements as PDF files. The parser focuses on the executed trades section and ignores dividends, deposits, withdrawals, and interest for now.

The current PDF parser uses the account-currency value shown in the statement rows. With CZK statements, no manual FX rate is needed for imported trades.

Supported rate formats:

```text
USD=23.20
EUR=25.10
USD:2025=23.42
EUR:2025=25.05
```

Year-specific entries such as `USD:2025` override a generic `USD` rate.

## Current Scope

- Trading 212 activity statement PDF import
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

The web app has a Gmail import panel in the sidebar. On first use, paste the downloaded Google OAuth `credentials.json` into the setup box and save it. The app stores the OAuth client config and Gmail refresh token under `.invest_tax_calc/`, which is ignored by git.

After setup, clicking `Connect Gmail` opens the Google login/approval flow when needed. If a saved read-only Gmail token is still valid, the app imports directly without opening a browser tab.

The CLI importer still supports environment variables:

```powershell
$env:GMAIL_CLIENT_ID="your-client-id.apps.googleusercontent.com"
$env:GMAIL_CLIENT_SECRET="your-client-secret"
uv run python -m invest_tax_calc.email_import --query "from:trading212 has:attachment filename:pdf"
```

The Gmail connector requests exactly:

```text
https://www.googleapis.com/auth/gmail.readonly
```

Attachments are downloaded into `imports/email_reports/`. Imported attachment hashes are recorded in `.invest_tax_calc/email_import_ledger.json`, so re-running an import reuses already-downloaded PDFs by SHA-256 hash while still analyzing them. Parsed PDF trades are cached under `.invest_tax_calc/parsed_pdf_cache/`; use the `Re-parse cached PDFs` option after changing the parser.

When downloaded attachments are PDFs, the web app loads all matching PDF statements into the analyzer automatically.
